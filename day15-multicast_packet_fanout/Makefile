PYTHON ?= python3
IVERILOG ?= iverilog
VVP ?= vvp
VERILATOR ?= verilator
YOSYS ?= yosys

RTL := multicast_packet_fanout.sv fanout_status.svh
TB := tb_multicast_packet_fanout.sv
PROFILES := default odd solo
IMAGES := $(addsuffix .vvp,$(addprefix build/,$(PROFILES)))
SOURCES := fanout_model.py fanout_verify.py

.PHONY: test lint synth formal waveform clean

test: $(IMAGES)
	$(PYTHON) -B fanout_verify.py example --image build/default.vvp --simulator "$(VVP)" --directory build/example
	$(PYTHON) -B -m unittest -v test_fanout_audit
	for profile in $(PROFILES); do \
		$(PYTHON) -B fanout_verify.py check --profile $$profile --image build/$$profile.vvp --simulator "$(VVP)" --directory build/$$profile || exit 1; \
		$(PYTHON) -B fanout_audit.py --profile $$profile build/$$profile/inputs.hex build/$$profile/observed.hex --output build/$$profile/audit.json || exit 1; \
	done

build/%.vvp: $(RTL) rtl.f $(TB) $(SOURCES)
	mkdir -p build
	params=$$($(PYTHON) -B fanout_verify.py parameters --profile $*) && \
	$(IVERILOG) -g2012 -s tb_multicast_packet_fanout $$params -o $@ -f rtl.f $(TB)

lint:
	$(VERILATOR) --lint-only -Wall --top-module multicast_packet_fanout -f rtl.f

synth:
	mkdir -p build
	$(YOSYS) -Q -T -l build/synth.log -q -p 'read_verilog -sv multicast_packet_fanout.sv; synth -top multicast_packet_fanout; check -assert; stat'
	$(PYTHON) -c 'from pathlib import Path; print(Path("build/synth.log").read_text().rsplit("=== multicast_packet_fanout ===", 1)[-1].strip())'

formal: formal/formal_multicast_packet_fanout.sv $(RTL)
	mkdir -p build
	$(YOSYS) -Q -T -l build/formal.log -q -p 'read_verilog -formal -sv multicast_packet_fanout.sv formal/formal_multicast_packet_fanout.sv; hierarchy -check -top formal_multicast_packet_fanout; proc; flatten; memory_map; async2sync; chformal -lower; opt; sat -seq 9 -set-at 1 rst 1 -prove-asserts -verify'
	@grep 'SUCCESS' build/formal.log

waveform: build/default.vvp ../scripts/render_vcd_waveform.py
	mkdir -p docs
	$(PYTHON) -B fanout_verify.py example --image build/default.vvp --simulator "$(VVP)" --directory build/example --waveform build/fanout.vcd
	$(PYTHON) -B ../scripts/render_vcd_waveform.py build/fanout.vcd docs/waveform.svg \
		--start 32 --end 150 --title 'Day 15 · A slow subscriber retains its own packet copy' \
		--signal 'tb_multicast_packet_fanout.clk=Clock' \
		--signal 'tb_multicast_packet_fanout.s_valid=Input valid' \
		--signal 'tb_multicast_packet_fanout.s_last=Input last' \
		--signal 'tb_multicast_packet_fanout.s_tag=Input tag' \
		--signal 'tb_multicast_packet_fanout.m_valid=Output valid mask' \
		--signal 'tb_multicast_packet_fanout.m_ready=Output ready mask' \
		--signal 'tb_multicast_packet_fanout.m_last=Output last mask' \
		--signal 'tb_multicast_packet_fanout.resident_slots=Live shared slots' \
		--signal 'tb_multicast_packet_fanout.report_valid=Report valid' \
		--signal 'tb_multicast_packet_fanout.report_status=Report status'

clean:
	rm -rf build obj_dir
