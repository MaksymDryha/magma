"""
Copyright 2020 The Magma Authors.

This source code is licensed under the BSD-style license found in the
LICENSE file in the root directory of this source tree.

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

from typing import List  # noqa
import os
import shlex
import subprocess
import logging
from lte.protos.policydb_pb2 import FlowMatch
from .types import QosInfo
from .utils import IdManager

LOG = logging.getLogger('pipelined.qos.qos_tc_impl')
#LOG.setLevel(logging.DEBUG)

# this code can run in either a docker container(CWAG) or as a native
# python process(AG). When we are running as a root there is no need for
# using sudo. (related to task T63499189 where tc commands failed since
# sudo wasn't available in the docker container)
def argSplit(cmd: str) -> List[str]:
    args = [] if os.geteuid() == 0 else ["sudo"]
    args.extend(shlex.split(cmd))
    return args


def run_cmd(cmd_list, show_error=True):
    for cmd in cmd_list:
        LOG.debug("running %s", cmd)
        try:
            LOG.debug(cmd)
            args = argSplit(cmd)
            subprocess.check_call(args)
        except subprocess.CalledProcessError as e:
            if show_error:
                LOG.error("%s error running %s ", str(e), cmd)


# TODO - replace this implementation with pyroute2 tc
ROOT_QID = 65534
DEFAULT_RATE = '12Kbit'
class TrafficClass:
    """
    Creates/Deletes queues in linux. Using Qdiscs for flow based
    rate limiting(traffic shaping) of user traffic.
    """

    @staticmethod
    def delete_class(intf: str, qid: int, show_error=True) -> None:
        qid_hex = hex(qid)
        # delete filter if this is a leaf class
        filter_cmd = "tc filter del dev {intf} protocol ip parent 1: prio 1 "
        filter_cmd += "handle {qid} fw flowid 1:{qid}"
        filter_cmd = filter_cmd.format(intf=intf, qid=qid_hex)
        run_cmd([filter_cmd], show_error)

        # delete class
        tc_cmd = "tc class del dev {intf} classid 1:{qid}".format(intf=intf,
            qid=qid_hex)
        run_cmd([tc_cmd], show_error)


    @staticmethod
    def create_class(intf: str, qid: int, max_bw: int, rate=None,
                     parent_qid=None, show_error=True) -> None:
        if not rate:
            rate = DEFAULT_RATE

        if not parent_qid:
            parent_qid = ROOT_QID

        qid_hex = hex(qid)
        parent_qid_hex = hex(parent_qid)
        tc_cmd = "tc class add dev {intf} parent 1:{parent_qid} "
        tc_cmd += "classid 1:{qid} htb rate {rate} ceil {maxbw}"
        tc_cmd = tc_cmd.format(intf=intf, parent_qid=parent_qid_hex,
            qid=qid_hex, rate=rate, maxbw=max_bw)

        # delete if exists
        TrafficClass.delete_class(intf, qid, show_error=False)

        run_cmd([tc_cmd], show_error=show_error)

        # add fq_codel and filter
        qdisc_cmd = "tc qdisc add dev {intf} parent 1:{qid} fq_codel"
        qdisc_cmd = qdisc_cmd.format(intf=intf, qid=qid_hex)
        filter_cmd = "tc filter add dev {intf} protocol ip parent 1: prio 1 "
        filter_cmd += "handle {qid} fw flowid 1:{qid}"
        filter_cmd = filter_cmd.format(intf=intf, qid=qid_hex)

        # add fq_codel qdisc and filter
        run_cmd((qdisc_cmd, filter_cmd), show_error)

    @staticmethod
    def init_qdisc(intf: str, show_error=False) -> None:
        speed = '1000'
        qid_hex = hex(ROOT_QID)
        with open("/sys/class/net/{intf}/speed".format(intf=intf)) as f:
            speed = f.read().strip()

        qdisc_cmd = "tc qdisc add dev {intf} root handle 1: htb".format(intf=intf)
        parent_q_cmd = "tc class add dev {intf} parent 1: classid 1:{root_qid} htb "
        parent_q_cmd +="rate {speed}Mbit ceil {speed}Mbit"
        parent_q_cmd = parent_q_cmd.format(intf=intf, root_qid=qid_hex, speed=speed)
        tc_cmd = "tc class add dev {intf} parent 1:{root_qid} classid 1:1 htb "
        tc_cmd += "rate {rate} ceil {speed}Mbit"
        tc_cmd = tc_cmd.format(intf=intf, root_qid=qid_hex, rate=DEFAULT_RATE,
                               speed=speed)
        run_cmd((qdisc_cmd, parent_q_cmd, tc_cmd), show_error)

    @staticmethod
    def read_all_classes(intf: str):
        qid_list = []
        # example output of this command
        # b'class htb 1:1 parent 1:fffe prio 0 rate 12Kbit ceil 1Gbit burst \
        # 1599b cburst 1375b \nclass htb 1:fffe root rate 1Gbit ceil 1Gbit \
        # burst 1375b cburst 1375b \n'
        # we need to parse this output and extract class ids from here
        tc_cmd = "tc class show dev {}".format(intf)
        args = argSplit(tc_cmd)
        try:
            output = subprocess.check_output(args)
            for ln in output.decode('utf-8').split("\n"):
                ln = ln.strip()
                if not ln:
                    continue
                tok = ln.split()
                if len(tok) < 5:
                    continue

                if tok[1] != "htb":
                    continue

                if tok[3] == 'root':
                    continue

                qid_str = tok[2].split(':')[1]
                qid = int(qid_str, 16)

                pqid_str = tok[4].split(':')[1]
                pqid = int(pqid_str, 16)
                qid_list.append((qid, pqid))

        except subprocess.CalledProcessError as e:
            LOG.error('failed extracting classids from tc %s', str(e))
        return qid_list

    @staticmethod
    def dump_class_state(intf: str, qid: int):
        qid_hex = hex(qid)
        tc_cmd = "tc -s -d class show dev {} classid 1:{}".format(intf,
                                                                  qid_hex)
        args = argSplit(tc_cmd)
        try:
            output = subprocess.check_output(args)
            print(output.decode())
        except subprocess.CalledProcessError:
            print("Exception dumping Qos State for %s", intf)


class TCManager(object):
    """
    Creates/Deletes queues in linux. Using Qdiscs for flow based
    rate limiting(traffic shaping) of user traffic.
    Queues are created on an egress interface and flows
    in OVS are programmed with qid to filter traffic to the queue.
    Traffic matching a specific flow is filtered to a queue and is
    rate limited based on configured value.
    Traffic to flows with no QoS configuration are sent to a
    default queue and are not rate limited.
    """
    def __init__(self,
                 datapath,
                 loop,
                 config) -> None:
        self._datapath = datapath
        self._loop = loop
        self._uplink = config['nat_iface']
        self._downlink = config['enodeb_iface']
        self._max_rate = config["qos"]["max_rate"]
        self._start_idx, self._max_idx = (config['qos']['linux_tc']['min_idx'],
                                          config['qos']['linux_tc']['max_idx'])
        self._id_manager = IdManager(self._start_idx, self._max_idx)
        self._initialized = True
        LOG.info("Init LinuxTC module uplink:%s downlink:%s",
                 config['nat_iface'], config['enodeb_iface'])

    def destroy(self,):
        LOG.info("destroying existing qos classes")
        # ensure ordering during deletion of classes, children should be deleted
        # prior to the parent class ids
        for intf in [self._uplink, self._downlink]:
            qid_list   = TrafficClass.read_all_classes(intf)
            for qid_tuple in qid_list :
                (qid, pqid) = qid_tuple
                if qid >= self._start_idx and qid < (self._max_idx - 1):
                    LOG.info("Attemting to delete class idx %d", qid)
                    TrafficClass.delete_class(intf, qid, show_error=False)
                if pqid >= self._start_idx and pqid < (self._max_idx - 1):
                    LOG.info("Attemting to delete parent class idx %d", pqid)
                    TrafficClass.delete_class(intf, pqid, show_error=False)

    def setup(self,):
        # initialize new qdisc
        TrafficClass.init_qdisc(self._uplink)
        TrafficClass.init_qdisc(self._downlink)

    def get_action_instruction(self, qid: int):
        # return an action and an instruction corresponding to this qid
        if qid < self._start_idx or qid > (self._max_idx - 1):
            LOG.error("invalid qid %d, no action/inst returned", qid)
            return

        parser = self._datapath.ofproto_parser
        return (parser.OFPActionSetField(pkt_mark=qid), None)

    def add_qos(self, d: FlowMatch.Direction, qos_info: QosInfo,
        parent=None) -> int:
        qid = self._id_manager.allocate_idx()
        intf = self._uplink if d == FlowMatch.UPLINK else self._downlink
        TrafficClass.create_class(intf, qid, qos_info.mbr, rate=qos_info.gbr,
            parent_qid=parent)
        return qid

    def remove_qos(self, qid: int, d: FlowMatch.Direction,
                   recovery_mode=False):
        if not self._initialized and not recovery_mode:
            return

        if qid < self._start_idx or qid > (self._max_idx - 1):
            LOG.error("invalid qid %d, removal failed", qid)
            return

        LOG.debug("deleting qos_handle %s", qid)
        intf = self._uplink if d == FlowMatch.UPLINK else self._downlink
        TrafficClass.delete_class(intf, qid)
        self._id_manager.release_idx(qid)

    def read_all_state(self, ):
        LOG.debug("read_all_state")
        st = {}
        ul_qid_list  = TrafficClass.read_all_classes(self._uplink)
        dl_qid_list = TrafficClass.read_all_classes(self._downlink)
        for(d, qid_list) in ((FlowMatch.UPLINK, ul_qid_list),
                             (FlowMatch.DOWNLINK, dl_qid_list)):
            for qid_tuple in qid_list:
                qid, pqid = qid_tuple
                if qid < self._start_idx or qid > (self._max_idx - 1):
                    continue
                st[qid] = {
                    'direction': d,
                    'ambr': pqid if pqid != self._max_idx else 0,
                }

        self._id_manager.restore_state(st)
        fut = self._loop.create_future()
        LOG.debug("map -> %s", st)
        fut.set_result(st)
        return fut
