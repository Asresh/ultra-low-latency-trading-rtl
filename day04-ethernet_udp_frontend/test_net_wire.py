from dataclasses import replace
from ipaddress import IPv4Address
import random
import struct
import unittest

from net_wire import (
    MacBeat, Match, Packet, Result, Rule, TxDescriptor, as_mac, bit_fields, checksum, classify,
    from_beats, parse_frame, to_beats, udp_checksum, UDP_HEAD,
)
from net_scenarios import DST, SRC, standard


def patch_word(data, pos, value):
    """Replace a 16-bit packet field without altering the rest of the frame."""
    return data[:pos] + struct.pack(">H",value) + data[pos+2:]


class IntegrityTests(unittest.TestCase):
    def test_rfc1071_example(self):
        """Check that RFC 1071 example."""
        octets = bytes.fromhex("0001f203f4f5f6f7")
        self.assertEqual(checksum(octets),0x220d)
        self.assertEqual(checksum(octets+bytes.fromhex("220d")),0)

    def test_known_vlan_wire_frame(self):
        """Check that known VLAN wire frame."""
        frame = bytes.fromhex(
            "0000000000010000000000028100007b08004500002100004000401140c9"
            "0a000001ef010101007b01c8000dbfbb68656c6c6f000000000000000000"
        )
        decoded = parse_frame(frame)
        self.assertEqual(decoded.result,Result.OK)
        self.assertEqual(decoded.packet,Packet(
            1,2,0x0a000001,0xef010101,123,456,b"hello",((0x8100,123),)
        ))
        self.assertEqual(decoded.packet.serialize(),frame)

    def test_udp_pseudo_header_detects_source_rewrite(self):
        """Check that UDP pseudo header detects source rewrite."""
        p = standard(b"odd UDP payload")
        source_rewritten = replace(p,src_ip=int(IPv4Address("10.0.0.2")))
        original_udp = p.segment()
        rewritten = source_rewritten.l2()+source_rewritten.ip_header(len(original_udp))+original_udp
        self.assertEqual(parse_frame(rewritten).result,Result.UDP_CHECKSUM)

    def test_ieee_padding_excluded_from_udp_checksum(self):
        """Check that ieee padding excluded from UDP checksum."""
        packet = standard(b"x")
        raw = packet.serialize(include_pad=False)
        for fill in (0,0x22,255):
            padded = packet.serialize(fill=fill)
            self.assertEqual(parse_frame(padded).packet,packet)
            self.assertEqual(parse_frame(raw).packet,packet)
        self.assertEqual(parse_frame(padded+b"\x00").result,Result.IP_LENGTH)

    def test_checksum_option_applies_only_to_ipv4_zero_field(self):
        """Check that checksum option applies only to IPv4 zero field."""
        p = standard(b"odd",udp_zero=True)
        raw = p.serialize()
        self.assertEqual(parse_frame(raw).result,Result.OK)
        self.assertEqual(parse_frame(raw,allow_zero=False).result,Result.UDP_ZERO)
        with_sum = replace(p,udp_zero=False).serialize()
        tampered = with_sum[:43]+bytes((with_sum[43]^0xf1,))+with_sum[44:]
        self.assertEqual(parse_frame(tampered).result,Result.UDP_CHECKSUM)

    def test_computed_zero_udp_checksum_transmitted_as_all_ones(self):
        """Check that computed zero UDP checksum transmitted as all ones."""
        p = standard(b"\x00\x00")
        segment = UDP_HEAD.pack(p.src_port,p.dst_port,10,0)+p.payload
        fix = udp_checksum(p.src_ip,p.dst_ip,segment)
        fixed = replace(p,payload=fix.to_bytes(2,"big"))
        self.assertEqual(fixed.segment()[6:8],b"\xff\xff")
        self.assertEqual(parse_frame(fixed.serialize(),allow_zero=False).result,Result.OK)

    def test_checksums_options_lengths_and_byte_alignments(self):
        """Check that checksums options lengths and byte alignments."""
        r = random.Random(47)
        for option_len in range(0,41,4):
            for vlan_count in range(3):
                tags = tuple((0x8100,40+i) for i in range(vlan_count))
                for body_len in (0,1,2,7,8,17,45,62):
                    with self.subTest(vlan=vlan_count,options=option_len,length=body_len):
                        p = standard(bytes(r.randrange(256) for _ in range(body_len)),
                                     tags=tags,options=b"\x01"*option_len,ident=r.randrange(65536))
                        frame = p.serialize()
                        self.assertEqual(parse_frame(frame).packet,p)
                        if body_len:
                            broken = bytearray(frame)
                            body_start = len(p.l2())+20+option_len+8
                            broken[body_start+body_len-1] ^= 1
                            self.assertEqual(parse_frame(bytes(broken)).result,Result.UDP_CHECKSUM)

    def test_length_and_fragment_errors_have_explicit_codes(self):
        """Check that length and fragment errors have explicit codes."""
        p = standard(b"z"*34)
        wire = p.serialize()
        variants = [
            (patch_word(wire,16,19),Result.IP_LENGTH),
            (patch_word(wire,16,0xffff),Result.IP_LENGTH),
            (patch_word(wire,20,0x4010),Result.IP_FRAGMENT),
            (patch_word(wire,20,0x2000),Result.IP_FRAGMENT),
            (patch_word(wire,20,0x8000),Result.IP_FRAGMENT),
            (patch_word(wire,38,0),Result.UDP_LENGTH),
            (patch_word(wire,38,65535),Result.UDP_LENGTH),
            (replace(p,protocol=6).serialize(),Result.IP_PROTOCOL),
            (replace(p,ttl=0).serialize(),Result.IP_SHAPE),
        ]
        for raw,code in variants:
            with self.subTest(code=code):
                self.assertEqual(parse_frame(raw).result,code)


class BoundariesTests(unittest.TestCase):
    def test_exact_packet_bound_and_following_excess(self):
        """Check that exact packet bound and following excess."""
        tags=((0x88a8,2),(0x8100,3))
        p=standard(bytes(1446),tags=tags,options=b"\x01"*40)
        frame=p.serialize()
        self.assertEqual(len(frame),1536)
        self.assertEqual(parse_frame(frame).packet,p)
        self.assertEqual(parse_frame(frame,max_frame=1535).result,Result.FRAME_LIMIT)
        self.assertEqual(parse_frame(frame+b"z").result,Result.FRAME_LIMIT)

    def test_transport_size_does_not_include_intentional_ethernet_gap(self):
        """Check that transport size does not include intentional ethernet gap."""
        p=standard(b"once")
        frame=p.serialize(include_pad=False)
        for padding_len in range(60-len(frame)+1):
            raw=frame+bytes([123])*padding_len
            self.assertEqual(parse_frame(raw).result,Result.OK)
        abnormal=frame.ljust(61,b"K")
        self.assertEqual(parse_frame(abnormal).result,Result.IP_LENGTH)

    def test_final_lane_fragments_are_orthogonal_to_rfc_udp_length(self):
        """Check that final lane fragments are orthogonal to rfc UDP length."""
        for depth in range(3):
            tags=tuple((0x8100,i) for i in range(depth))
            for valid_bytes in range(1,9):
                body=bytes(48+(valid_bytes-(42+depth*4))%8)
                p=standard(body,tags=tags)
                wire=p.serialize()
                beats=to_beats(wire)
                self.assertEqual(len(beats[-1].valid_bytes()),valid_bytes)
                reconstructed,code=from_beats(beats)
                self.assertEqual((reconstructed,code),(wire,Result.OK))
                self.assertEqual(parse_frame(reconstructed).packet.payload,body)


class FramingAndClassifierTests(unittest.TestCase):
    def test_every_final_keep_is_low_prefix(self):
        """Check that every final keep is low prefix."""
        for length in range(1,145):
            payload = bytes((i*19)&255 for i in range(length))
            rng = random.Random(length)
            beats = to_beats(payload,rng=rng)
            self.assertEqual(from_beats(beats),(payload,Result.OK))
            self.assertEqual(beats[-1].keep,(1 << ((length-1)%8+1))-1)
            self.assertTrue(beats[0].first)
            self.assertTrue(beats[-1].last)

    def test_frame_bad_priority_even_when_mac_bad(self):
        """Check that frame bad priority even when mac bad."""
        data = to_beats(b"longer than eight octets",bad=True)
        samples = [
            (0,{"first":False}),
            (1,{"first":True}),
            (1,{"keep":0xff,"last":True}),
            (0,{"keep":0x81}),
            (-1,{"keep":0}),
        ]
        for index,fields in samples:
            mutated = list(data)
            mutated[index] = replace(mutated[index],**fields)
            self.assertEqual(from_beats(mutated)[1],Result.BAD_AXIS)
        self.assertEqual(from_beats(data)[1],Result.MAC_BAD)

    def test_record_parser_rejects_out_of_range_lanes(self):
        """Check that record parser rejects out of range lanes."""
        reference = MacBeat(0x00335577,7,True,True)
        self.assertEqual(MacBeat.parse(reference.row()),reference)
        malformed = ("123 1 2 1 0","0 ff 1 0","10000000000000001 1 1 0 0","0 1ff 1 0 0")
        for row in malformed:
            with self.subTest(row=row),self.assertRaises(ValueError):
                MacBeat.parse(row)

    def test_msb_priority_uses_policy_order(self):
        """Check that MSB priority uses policy order."""
        tagged = standard(b"x",tags=((0x8100,0xc00a),))
        parsed = parse_frame(tagged.serialize())
        broad = Rule(dst_ip=Match(DST,0xffffff00),queue=2)
        narrow = Rule(tci=Match(10,0xfff),tags=Match(1,3),drop=True)
        self.assertEqual(classify(parsed,[broad,narrow],8).queue,2)
        self.assertEqual(classify(parsed,[narrow,broad],8).result,Result.RULE_DROP)
        disabled = replace(narrow,enabled=False)
        self.assertEqual(classify(parsed,[disabled],9).result,Result.RULE_MISS)
        first = Rule(src_ip=Match(SRC,0xffffffff),src_port=Match(12000,0xfff0),queue=5)
        self.assertEqual(classify(parsed,[first],7).queue,5)

    def test_metadata_fields_and_timestamp_mask(self):
        """Check that metadata fields and timestamp mask."""
        p = standard(b"abcdef",tags=((0x88a8,5),(0x8100,7)),ident=0x3344,ttl=3)
        parsed = classify(parse_frame(p.serialize()),[Rule(queue=6)],42)
        data,mask = parsed.rx_info()
        self.assertEqual(data>>360,0x062a)
        self.assertEqual(data>>352 & 255,22)
        self.assertEqual(mask>>288 & ((1<<64)-1),0)
        self.assertEqual(data>>80 & 65535,6)
        self.assertEqual(data & 0xff,0x95)
        bad,known_mask = parse_frame(b"short").rx_info()
        self.assertEqual(bad>>376,Result.L2_SHORT)
        self.assertEqual(known_mask>>376,255)

    def test_numeric_inputs_are_bounded(self):
        """Check that numeric inputs are bounded."""
        self.assertEqual(as_mac("01:00:5e:01:02:03"),0x1005e010203)
        for value in ("1:00:5e:01:02:03","zz:00:00:00:00:00",1<<48,[]):
            with self.subTest(value=value),self.assertRaises(ValueError):
                as_mac(value)
        with self.assertRaises(ValueError):
            bit_fields((0,6),(64,6))


class TxContractTests(unittest.TestCase):
    def test_descriptor_packing_respects_network_and_host_field_widths(self):
        """Check that descriptor packing respects network and host field widths."""
        p = standard(b"abc",ident=0xabcd,ttl=2,tos=0xb8,udp_zero=True,df=True)
        word = TxDescriptor(p,0x00112233).encode()
        self.assertEqual(word>>288,0x00112233)
        self.assertEqual(word>>80 & 65535,3)
        self.assertEqual(word>>32 & 65535,0xabcd)
        self.assertEqual(word & 0xffffffff,0x02b80030)

    def test_descriptor_reject_preserves_failure_precedence(self):
        """Check that descriptor reject preserves failure precedence."""
        p = standard(b"ninebytes")
        beats = to_beats(p.payload)
        beats[0] = replace(beats[0],keep=0)
        descriptor = TxDescriptor(replace(p,ttl=0),0x1234)
        self.assertEqual(descriptor.expected(beats)[0],Result.TX_DESCRIPTOR)
        self.assertEqual(TxDescriptor(p,0x1234).expected(beats)[0],Result.BAD_AXIS)
        good_beats = to_beats(p.payload)
        sized = TxDescriptor(p,0x1234,payload_size=len(p.payload)+1)
        self.assertEqual(sized.expected(good_beats)[0],Result.TX_LENGTH)

    def test_host_tx_model_strips_options_and_builds_two_tag_header(self):
        """Check that host tx model strips options and builds two tag header."""
        p = standard(b"body",tags=((0x8100,3),(0x8100,8)),options=b"\x01"*12,protocol=6)
        status,wire = TxDescriptor(p,5).expected(to_beats(p.payload))
        self.assertEqual(status,Result.OK)
        result = parse_frame(wire).packet
        self.assertEqual(result.tags,((0x88a8,3),(0x8100,8)))
        self.assertEqual(result.options,b"")
        self.assertEqual(result.protocol,17)


if __name__ == "__main__":
    unittest.main()
