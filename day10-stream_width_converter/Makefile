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

test: integrity-test $(BUILD)/faults.vvp $(BUILD)/duplex.vvp $(BUILD)/stream_up.vvp $(BUILD)/stream_odd.vvp $(BUILD)/up $(BUILD)/down $(BUILD)/odd $(BUILD)/tiny
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

lint: integrity-lint
	$(VERILATOR) --lint-only -Wall --top-module $(TOP) -f rtl.f
	$(VERILATOR) --lint-only -Wall --top-module streaming_gearbox -f rtl.f

synth: integrity-synth | $(BUILD)
	$(YOSYS) -Q -q -l $(BUILD)/synth.log -m slang -p 'read_slang --top $(TOP) $(RTL); synth -top $(TOP) -noabc; check -assert; stat'
	$(YOSYS) -Q -q -l $(BUILD)/streaming-synth.log -m slang -p 'read_slang --top streaming_gearbox $(RTL); synth -top streaming_gearbox -noabc; check -assert; stat'
	tail -n 20 $(BUILD)/synth.log
	tail -n 20 $(BUILD)/streaming-synth.log

formal: | $(BUILD)
	cd formal && $(SBY) -f --prefix ../$(BUILD)/byte_order byte_order.sby bmc cover

clean:
	rm -rf $(BUILD)

.PHONY: waveform

waveform: $(BUILD)/up ../scripts/render_vcd_waveform.py integrity-waveform
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
