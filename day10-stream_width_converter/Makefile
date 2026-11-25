PYTHON ?= python3
IVERILOG ?= iverilog
VVP ?= vvp
VERILATOR ?= verilator
YOSYS ?= yosys
SBY ?= sby
BUILD := build
TOP := stream_width_converter
RTL := $(shell cat rtl.f)

.PHONY: all test lint synth formal clean
all: test

$(BUILD):
	mkdir -p $@

define lane
$(BUILD)/$(1): tb_packet_gearbox.sv $(RTL) rtl.f | $(BUILD)
	$(IVERILOG) -g2012 -s tb_packet_gearbox -Ptb_packet_gearbox.IN_BYTES=$(2) -Ptb_packet_gearbox.OUT_BYTES=$(3) -Ptb_packet_gearbox.MAX_FRAME=$(4) -o $$@ -f rtl.f $$<
endef
$(eval $(call lane,up,4,8,128))
$(eval $(call lane,down,8,4,128))
$(eval $(call lane,odd,3,5,31))
$(eval $(call lane,tiny,1,2,3))

$(BUILD)/stream_up.vvp: tb_streaming_gearbox.sv streaming_gearbox.sv | $(BUILD)
	$(IVERILOG) -g2012 -s tb_streaming_gearbox -o $@ $^

$(BUILD)/stream_odd.vvp: tb_streaming_gearbox.sv streaming_gearbox.sv | $(BUILD)
	$(IVERILOG) -g2012 -s tb_streaming_gearbox -Ptb_streaming_gearbox.IN_BYTES=5 -Ptb_streaming_gearbox.OUT_BYTES=3 -Ptb_streaming_gearbox.CAPACITY=11 -o $@ $^

$(BUILD)/duplex.vvp: tb_stream_width_converter.sv $(RTL) rtl.f | $(BUILD)
	$(IVERILOG) -g2012 -s tb_stream_width_converter -o $@ -f rtl.f $<

$(BUILD)/faults.vvp: tb_gearbox_faults.sv packet_gearbox.sv | $(BUILD)
	$(IVERILOG) -g2012 -s tb_gearbox_faults -o $@ $^

test: integrity-test async-test splice-test $(BUILD)/faults.vvp $(BUILD)/duplex.vvp $(BUILD)/stream_up.vvp $(BUILD)/stream_odd.vvp $(BUILD)/up $(BUILD)/down $(BUILD)/odd $(BUILD)/tiny
	$(PYTHON) -B -m unittest discover -v
	$(VVP) $(BUILD)/faults.vvp
	$(VVP) $(BUILD)/duplex.vvp
	$(PYTHON) -B width_stream.py $(BUILD)
	@set -e; for profile in up odd; do $(VVP) $(BUILD)/stream_$$profile.vvp +tape=$(BUILD)/stream_$$profile.txt; done
	$(PYTHON) -B width_workload.py $(BUILD)
	@set -e; for profile in up down odd tiny; do $(VVP) $(BUILD)/$$profile +tape=$(BUILD)/$$profile.txt +trace=$(BUILD)/$$profile.trace; done
	$(PYTHON) -B width_audit.py $(BUILD)
	$(PYTHON) -B width_capture.py pack $(BUILD)/up.trace $(BUILD)/up.wgc --input-width 4 --output-width 8 --capacity 128
	$(PYTHON) -B width_capture.py check $(BUILD)/up.wgc
	$(PYTHON) -B width_chain.py $(BUILD)/chain --iverilog "$(IVERILOG)" --vvp "$(VVP)"
	$(PYTHON) -B width_demo.py $(BUILD)/up $(BUILD)/lesson --vvp "$(VVP)"
	$(PYTHON) -B width_campaign.py $(BUILD)/campaign --iverilog "$(IVERILOG)" --vvp "$(VVP)"

lint: integrity-lint async-lint splice-lint
	$(VERILATOR) --lint-only -Wall --top-module $(TOP) -f rtl.f
	$(VERILATOR) --lint-only -Wall --top-module streaming_gearbox -f rtl.f

synth: integrity-synth async-synth splice-synth | $(BUILD)
	$(YOSYS) -Q -q -l $(BUILD)/synth.log -m slang -p 'read_slang --top $(TOP) $(RTL); synth -top $(TOP) -noabc; check -assert; stat'
	$(YOSYS) -Q -q -l $(BUILD)/streaming-synth.log -m slang -p 'read_slang --top streaming_gearbox $(RTL); synth -top streaming_gearbox -noabc; check -assert; stat'
	tail -n 20 $(BUILD)/synth.log
	tail -n 20 $(BUILD)/streaming-synth.log

formal: splice-formal | $(BUILD)
	cd formal && $(SBY) -f --prefix ../$(BUILD)/byte_order byte_order.sby bmc cover

clean:
	rm -rf $(BUILD)

.PHONY: waveform

waveform: $(BUILD)/up ../scripts/render_vcd_waveform.py integrity-waveform async-waveform splice-waveform
	$(PYTHON) -B width_demo.py $< $(BUILD)/waveform --vvp "$(VVP)" --waveform $(BUILD)/stream_width_converter.vcd
	mkdir -p docs
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/stream_width_converter.vcd docs/waveform.svg \
		--start 20 --end 180 --title 'Day 10 · Coalesce valid bytes and reject a bad mask' \
		--signal 'tb_packet_gearbox.clk=Clock' \
		--signal 'tb_packet_gearbox.s_valid=Input valid' \
		--signal 'tb_packet_gearbox.s_ready=Input ready' \
		--signal 'tb_packet_gearbox.s_keep=Input byte mask' \
		--signal 'tb_packet_gearbox.s_last=Input last' \
		--signal 'tb_packet_gearbox.m_valid=Output valid' \
		--signal 'tb_packet_gearbox.m_ready=Output ready' \
		--signal 'tb_packet_gearbox.m_keep=Output byte mask' \
		--signal 'tb_packet_gearbox.m_last=Output last' \
		--signal 'tb_packet_gearbox.report_valid=Report valid' \
		--signal 'tb_packet_gearbox.report_status=Report status'

INTEGRITY_RTL := integrity_gearbox.sv packet_gearbox.sv
INTEGRITY_TB := tb_integrity_gearbox
.PHONY: integrity-test integrity-lint integrity-synth integrity-waveform

define integrity
$(BUILD)/integrity-$(1).vvp: $(INTEGRITY_TB).sv $(INTEGRITY_RTL) | $(BUILD)
	$(IVERILOG) -g2012 -s $(INTEGRITY_TB) \
		-P$(INTEGRITY_TB).IN_BYTES=$(2) -P$(INTEGRITY_TB).OUT_BYTES=$(3) -P$(INTEGRITY_TB).MAX_FRAME=$(4) -o $$@ $$^

$(BUILD)/integrity-$(1)/audit.txt: integrity_vectors.py | $(BUILD)
	$(PYTHON) -B integrity_vectors.py $(BUILD)/integrity-$(1) --in-bytes $(2) --out-bytes $(3) --capacity $(4) --seed $(5)

.PHONY: integrity-$(1)
integrity-$(1): $(BUILD)/integrity-$(1).vvp $(BUILD)/integrity-$(1)/audit.txt
	$(VVP) $$< +tape=$(BUILD)/integrity-$(1)
endef

$(eval $(call integrity,main,4,8,128,90211))
$(eval $(call integrity,odd,5,3,61,63577))
$(eval $(call integrity,tiny,1,2,3,98237))

integrity-test: integrity-main integrity-odd integrity-tiny

integrity-lint:
	$(VERILATOR) --lint-only -Wall --top-module integrity_gearbox $(INTEGRITY_RTL)

integrity-synth: | $(BUILD)
	$(YOSYS) -Q -q -l $(BUILD)/integrity-synth.log -m slang -p 'read_slang --top integrity_gearbox $(INTEGRITY_RTL); synth -top integrity_gearbox -noabc; check -assert; stat'

integrity-waveform: $(BUILD)/integrity-main.vvp $(BUILD)/integrity-main/audit.txt
	$(VVP) $< +tape=$(BUILD)/integrity-main +wave=$(BUILD)/integrity.vcd
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/integrity.vcd docs/integrity.svg \
		--start 30 --end 350 --title 'Width converter · CRC verification and packet release' \
		--signal 'tb_integrity_gearbox.clk=Clock' \
		--signal 'tb_integrity_gearbox.s_last=Input last' \
		--signal 'tb_integrity_gearbox.s_valid=Input valid' \
		--signal 'tb_integrity_gearbox.s_ready=Input ready' \
		--signal 'tb_integrity_gearbox.audit_valid=Audit valid' \
		--signal 'tb_integrity_gearbox.audit_status=Audit status' \
		--signal 'tb_integrity_gearbox.m_valid=Packet valid' \
		--signal 'tb_integrity_gearbox.m_ready=Packet ready' \
		--signal 'tb_integrity_gearbox.m_keep=Output byte mask'

ASYNC_COMMON := ../day06-multiclock_latency_telemetry
ASYNC_RTL := async_integrity_gearbox.sv $(INTEGRITY_RTL) $(ASYNC_COMMON)/latency_async_fifo.sv $(ASYNC_COMMON)/latency_reset_sync.sv
ASYNC_TB := tb_async_integrity_gearbox
.PHONY: async-test async-lint async-synth async-waveform

define async_case
$(BUILD)/async-$(1).vvp: $(ASYNC_TB).sv $(ASYNC_RTL) | $(BUILD)
	$(IVERILOG) -g2012 -s $(ASYNC_TB) -P$(ASYNC_TB).IN_BYTES=$(2) -P$(ASYNC_TB).OUT_BYTES=$(3) \
		-P$(ASYNC_TB).MAX_FRAME=$(4) -P$(ASYNC_TB).SOURCE_HALF=$(5) -P$(ASYNC_TB).DEST_HALF=$(6) \
		-P$(ASYNC_TB).IN_ADDR_W=$(7) -P$(ASYNC_TB).AUDIT_ADDR_W=$(8) -o $$@ $$^

$(BUILD)/async-$(1)/commands.txt: async_integrity_vectors.py integrity_vectors.py | $(BUILD)
	$(PYTHON) -B async_integrity_vectors.py $(BUILD)/async-$(1) --in-bytes $(2) --out-bytes $(3) \
		--capacity $(4) --seed $(9)

.PHONY: async-$(1)
async-$(1): $(BUILD)/async-$(1).vvp $(BUILD)/async-$(1)/commands.txt
	$(VVP) $$< +vectors=$(BUILD)/async-$(1)
endef

$(eval $(call async_case,main,4,8,64,3,5,2,1,98229))
$(eval $(call async_case,odd,5,3,61,7,3,1,3,97011))
$(eval $(call async_case,tiny,1,2,3,4,5,3,1,77541))

async-test: async-main async-odd async-tiny

async-lint:
	$(VERILATOR) --lint-only -Wall -Wno-SYNCASYNCNET --top-module async_integrity_gearbox $(ASYNC_RTL)

async-synth: | $(BUILD)
	$(YOSYS) -Q -q -l $(BUILD)/async-synth.log -m slang -p 'read_slang --top async_integrity_gearbox $(ASYNC_RTL); synth -top async_integrity_gearbox -noabc; check -assert; stat'

async-waveform: $(BUILD)/async-main.vvp $(BUILD)/async-main/commands.txt
	$(VVP) $< +vectors=$(BUILD)/async-main +wave=$(BUILD)/async.vcd
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/async.vcd docs/async.svg \
		--start 990 --end 1150 --title 'Width converter · A drain barrier waits on destination packet completion' \
		--signal '$(ASYNC_TB).barrier_valid=Source barrier valid' \
		--signal '$(ASYNC_TB).barrier_ready=Source barrier ready' \
		--signal '$(ASYNC_TB).dut.seen_barrier=Barrier at destination head' \
		--signal '$(ASYNC_TB).dut.emit_barrier=Destination acknowledgment permitted' \
		--signal '$(ASYNC_TB).m_valid=Destination packet valid' \
		--signal '$(ASYNC_TB).m_ready=Destination packet ready' \
		--signal '$(ASYNC_TB).m_last=Destination packet last' \
		--signal '$(ASYNC_TB).audit_valid=Source audit valid' \
		--signal '$(ASYNC_TB).audit_barrier=Source audit is barrier' \
		--signal '$(ASYNC_TB).audit_tag=Source audit tag'

SPLICE_RTL := spliced_packet_gearbox.sv
SPLICE_TB := tb_spliced_packet_gearbox
.PHONY: splice-test splice-lint splice-synth splice-formal splice-waveform

define splice_case
$(BUILD)/splice-$(1).vvp: $(SPLICE_TB).sv $(SPLICE_RTL) | $(BUILD)
	$(IVERILOG) -g2012 -s $(SPLICE_TB) -P$(SPLICE_TB).IN_BYTES=$(2) -P$(SPLICE_TB).OUT_BYTES=$(3) \
		-P$(SPLICE_TB).MAX_FRAME=$(4) -P$(SPLICE_TB).MAX_PREFIX=$(5) -o $$@ $$^

$(BUILD)/splice-$(1)/commands.txt: splice_vectors.py | $(BUILD)
	$(PYTHON) -B splice_vectors.py $(BUILD)/splice-$(1) --in-width $(2) --out-width $(3) \
		--capacity $(4) --prefix $(5) --seed $(6)

.PHONY: splice-$(1)
splice-$(1): $(BUILD)/splice-$(1).vvp $(BUILD)/splice-$(1)/commands.txt
	$(VVP) $$< +vectors=$(BUILD)/splice-$(1)
endef

$(eval $(call splice_case,main,8,5,128,8,76532))
$(eval $(call splice_case,odd,3,7,31,3,27442))
$(eval $(call splice_case,tiny,1,2,3,2,84403))

splice-test: splice-main splice-odd splice-tiny

splice-lint:
	$(VERILATOR) --lint-only -Wall --top-module spliced_packet_gearbox $(SPLICE_RTL)

splice-synth: | $(BUILD)
	$(YOSYS) -Q -q -l $(BUILD)/splice-synth.log -m slang -p 'read_slang --top spliced_packet_gearbox $(SPLICE_RTL); synth -top spliced_packet_gearbox -noabc; check -assert; stat'

splice-formal: | $(BUILD)
	cd formal && $(SBY) -f --prefix ../$(BUILD)/splice-formal splice.sby

splice-waveform: $(BUILD)/splice-main.vvp $(BUILD)/splice-main/commands.txt
	$(VVP) $< +vectors=$(BUILD)/splice-main +wave=$(BUILD)/splice.vcd
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/splice.vcd docs/splice.svg \
		--start 25 --end 460 --title 'Packet splice · Normalize an offset frame and append a CRC' \
		--signal '$(SPLICE_TB).edit_valid=Edit descriptor valid' --signal '$(SPLICE_TB).s_valid=Ingress beat valid' \
		--signal '$(SPLICE_TB).s_keep=Ingress byte lanes' --signal '$(SPLICE_TB).report_valid=Verdict valid' \
		--signal '$(SPLICE_TB).m_valid=Output beat valid' --signal '$(SPLICE_TB).m_ready=Output beat ready' \
		--signal '$(SPLICE_TB).m_keep=Output byte lanes' --signal '$(SPLICE_TB).m_data=Edited data' \
		--signal '$(SPLICE_TB).dut.crc_after=Internal CRC state'
