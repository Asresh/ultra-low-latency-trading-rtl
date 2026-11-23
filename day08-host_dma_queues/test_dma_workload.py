from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dma_audit import (
    AuditError, PacketCapture, assemble_stream, compare, completion_rows, main, monotonic_time,
)
from dma_model import Access, Phase
from dma_protocol import Completion, Opcode, Status, U64_MASK, packet_beats
from dma_workload import build_workload, exercise, export, verify_burst_shapes


def axis_rows(emissions):
    return [
        f"{p.queue} {p.tag} {p.cookie} {beat.data:#x} {beat.keep:#x} "
        f"{int(beat.first)} {int(beat.last)}"
        for p in emissions for beat in p.beats()
    ]


def workload(seed, count, depth):
    configs, entries = build_workload(seed,count,depth)
    return export(configs,entries,exercise(configs,entries,seed))


class WorkloadContract(unittest.TestCase):
    def test_seeded_traffic_exercises_every_status_in_both_queues(self):
        for trial,depth in ((19,2),(823,4),(77,8)):
            doc = workload(trial,70,depth)
            self.assertEqual(doc["summary"]["completed"],140)
            self.assertEqual(set(doc["summary"]["statuses"]),{status.name for status in Status})
            self.assertGreater(doc["summary"]["transactions"]["hold_b"],30)
            self.assertGreater(doc["summary"]["pull_bytes"],0)
            self.assertGreater(doc["summary"]["push_bytes"],0)
            self.assertEqual(doc["summary"]["completion_high_water"],[depth,depth])
            self.assertGreater(doc["summary"]["completion_capacity_blocked_edges"],1)

    def test_reproducible_json_and_concurrent_submission_seed(self):
        a = workload(7,28,2)
        b = workload(7,28,2)
        self.assertEqual(a,b)
        json_image=json.loads(json.dumps(a))
        self.assertEqual(json_image["summary"],a["summary"])
        self.assertNotEqual(a["expected_packets"],workload(8,28,2)["expected_packets"])
        retired = [item for item in a["transaction_schedule"] if item["event"]=="retire"]
        for queue in (0,1):
            counters=[item["cq_tail"] for item in retired if item["queue"]==queue]
            self.assertTrue(any(earlier>later for earlier,later in zip(counters,counters[1:])))

    def test_raises_for_unproductive_or_wrong_geometry(self):
        with self.assertRaises(ValueError):
            build_workload(1,1,2)
        with self.assertRaises(ValueError):
            build_workload(1,25,6)

    def test_independent_ledger_for_axi_channel_errors(self):
        address=Access(0,Phase.PUSH,"AW",0x1000,0)
        valid_data=Access(0,Phase.PUSH,"W",0x1000,0x1234,0x03,True)
        b=Access(0,Phase.PUSH,"B",0x1000)
        verify_burst_shapes([address,valid_data,b])
        failures=(
            ([valid_data,b],"without prior"),
            ([address,address],"previous burst"),
            ([address,b],"before last"),
            ([address,valid_data],"remains open"),
            ([address,replace(valid_data,last=False),b],"last marker"),
            ([address,replace(valid_data,address=0x1008),b],"mid-burst"),
            ([address,replace(valid_data,mask=0x05),b],"strobe"),
            ([address,valid_data,replace(b,address=0x2000)],"wrong address"),
            ([replace(address,address=0xff8,data=1)],"cannot cross"),
            ([replace(address,channel="BAD")],"unexpected"),
        )
        for trace,description in failures:
            with self.subTest(description=description),self.assertRaises((AssertionError,ValueError)):
                verify_burst_shapes(trace)


class CompletionAudit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.configs,cls.entries=build_workload(21,35,4)
        cls.replay=exercise(cls.configs,cls.entries,21)
        cls.ledger=export(cls.configs,cls.entries,cls.replay)
        cls.values=list(cls.replay.model.completions)
        cls.rows=axis_rows(cls.replay.model.emissions)

    def test_audit_exact_binary_completions_and_axis_rows(self):
        binary=[item.encode().hex() for item in self.values]
        results=compare(self.ledger,completion_rows(binary),assemble_stream(self.rows),
                        strict_schedule=True,check_clock=True)
        self.assertEqual(results["completion_count"],70)
        self.assertEqual(results["packet_count"],len(self.replay.model.emissions))
        self.assertEqual(results["copied_payload_bytes"],self.replay.model.pull_bytes)
        self.assertEqual(results["error_completions"],self.replay.model.rejected)

    def test_compare_accepts_cross_queue_schedule_change_only_outside_strict_mode(self):
        reorder=sorted(self.values,key=lambda record:record.queue,reverse=True)
        report=compare(self.ledger,reorder,assemble_stream(self.rows))
        self.assertEqual(report["queues"],[0,1])
        with self.assertRaisesRegex(AuditError,"retirement order"):
            compare(self.ledger,reorder,assemble_stream(self.rows),strict_schedule=True)

    def test_error_in_crc_tag_payload_or_identity_is_visible(self):
        altered=self.values.copy()
        altered[0]=replace(altered[0],crc=altered[0].crc^1)
        with self.assertRaisesRegex(AuditError,"completion .* mismatch"):
            compare(self.ledger,altered,assemble_stream(self.rows))
        data=list(assemble_stream(self.rows))
        data[0]=replace(data[0],payload=b"x"+data[0].payload)
        with self.assertRaisesRegex(AuditError,"packets differ"):
            compare(self.ledger,self.values,data)
        with self.assertRaisesRegex(AuditError,"completion count"):
            compare(self.ledger,self.values[:-1],data)

    def test_invalid_input_length_unrecognized_abi_and_backwards_time(self):
        with self.assertRaisesRegex(AuditError,"row 1"):
            list(completion_rows(["abcd"]))
        with self.assertRaisesRegex(AuditError,"unrecognized"):
            compare({"format":"unrelated"},[],[])
        ref=Completion(Status.OK,Opcode.FENCE,0,0,0,0,0,0)
        monotonic_time([replace(ref,timestamp=U64_MASK),replace(ref,timestamp=1)])
        with self.assertRaisesRegex(AuditError,"run backward"):
            monotonic_time([replace(ref,timestamp=123),replace(ref,timestamp=122)])

    def test_audit_cli_files_emit_measured_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            plan=path/"ledger.json"
            data=path/"completions.txt"
            stream=path/"axis.txt"
            plan.write_text(json.dumps(self.ledger))
            data.write_text("\n".join(item.encode().hex() for item in self.values)+"\n")
            stream.write_text("\n".join(self.rows)+"\n")
            out=io.StringIO()
            with patch("sys.argv",["dma_audit",str(plan),str(data),str(stream),"--strict-schedule"]), \
                 redirect_stdout(out):
                main()
            report=json.loads(out.getvalue())
            self.assertEqual(report["completion_count"],70)
            self.assertEqual(report["queues"],[0,1])


class StreamAudit(unittest.TestCase):
    def test_packet_assembly_with_tail_lane_mask(self):
        payload=b"123456789ABC"
        rows=[
            f"1 4 100 {beat.data:#x} {beat.keep:#x} {int(beat.first)} {int(beat.last)}"
            for beat in packet_beats(payload)
        ]
        packet,=assemble_stream(rows)
        self.assertEqual(packet,PacketCapture(1,4,100,payload))

    def test_packet_assembly_rejects_mid_frame_metadata_and_format_changes(self):
        header="0 1 2 0x44332211 0xff 1 0"
        valid_end="0 1 2 0x00000044 0x01 0 1"
        problems=(
            ([header],"incomplete"),
            ([valid_end],"missing first"),
            ([header,"1 1 2 0x44 0x1 0 1"],"sideband changed"),
            ([header,header],"new first"),
            (["0 1 2 0x1 0x1 1 0",valid_end],"partial keep"),
            (["0 1 2 0x2 0x5 1 1"],"contiguous keep"),
            (["0 1 2 0x44 0x1 2 1"],"marker must"),
            (["0 1 2 not-a-number 0x1 1 1"],"not an integer"),
        )
        for sample,wanted in problems:
            with self.subTest(sample=sample),self.assertRaisesRegex(AuditError,wanted):
                list(assemble_stream(sample))


if __name__ == "__main__":
    unittest.main()
