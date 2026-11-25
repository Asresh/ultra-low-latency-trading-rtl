PYTHON ?= python3
IVERILOG ?= iverilog
VVP ?= vvp
VERILATOR ?= verilator
YOSYS ?= yosys
SBY ?= sby

RTL := multicast_packet_fanout.sv fanout_status.svh
TB := tb_multicast_packet_fanout.sv
PROFILES := default odd solo
IMAGES := $(addsuffix .vvp,$(addprefix build/,$(PROFILES)))
SOURCES := fanout_model.py fanout_verify.py
POLICY_RTL := $(shell cat rtl_policy.f)
REPLAY_RTL := $(shell cat rtl_replay.f)

.PHONY: test test-policy test-replay lint synth formal formal-replay waveform waveform-policy waveform-replay clean

test: $(IMAGES) test-policy test-replay
	$(PYTHON) -B fanout_verify.py example --image build/default.vvp --simulator "$(VVP)" --directory build/example
	$(PYTHON) -B -m unittest -v test_fanout_audit
	for profile in $(PROFILES); do \
		$(PYTHON) -B fanout_verify.py check --profile $$profile --image build/$$profile.vvp --simulator "$(VVP)" --directory build/$$profile || exit 1; \
		$(PYTHON) -B fanout_audit.py --profile $$profile build/$$profile/inputs.hex build/$$profile/observed.hex --output build/$$profile/audit.json || exit 1; \
	done

test-policy:
	$(PYTHON) -B run_fanout_policy.py --iverilog "$(IVERILOG)" --vvp "$(VVP)" --example
	$(PYTHON) -B run_fanout_policy.py --iverilog "$(IVERILOG)" --vvp "$(VVP)"

test-replay:
	$(PYTHON) -B -m unittest -v test_replay_checkpoint
	$(PYTHON) -B replay_verify.py --iverilog "$(IVERILOG)" --vvp "$(VVP)"
	$(PYTHON) -B replay_verify.py --handoff --iverilog "$(IVERILOG)" --vvp "$(VVP)"

build/%.vvp: $(RTL) rtl.f $(TB) $(SOURCES)
	mkdir -p build
	params=$$($(PYTHON) -B fanout_verify.py parameters --profile $*) && \
	$(IVERILOG) -g2012 -s tb_multicast_packet_fanout $$params -o $@ -f rtl.f $(TB)

lint:
	$(VERILATOR) --lint-only -Wall --top-module multicast_packet_fanout -f rtl.f
	$(VERILATOR) --lint-only -Wall --top-module multicast_policy_fanout -f rtl_policy.f
	$(VERILATOR) --lint-only -Wall --top-module multicast_replay_buffer -f rtl_replay.f

synth:
	mkdir -p build
	$(YOSYS) -Q -T -l build/synth.log -q -p 'read_verilog -sv multicast_packet_fanout.sv; synth -top multicast_packet_fanout; check -assert; stat'
	$(PYTHON) -c 'from pathlib import Path; print(Path("build/synth.log").read_text().rsplit("=== multicast_packet_fanout ===", 1)[-1].strip())'
	$(YOSYS) -Q -T -l build/synth_policy.log -q -p 'read_verilog -sv $(POLICY_RTL); synth -top multicast_policy_fanout; check -assert; stat'
	$(YOSYS) -Q -T -l build/synth_replay.log -q -p 'read_verilog -sv $(REPLAY_RTL); synth -top multicast_replay_buffer -noabc; check -assert; stat; write_json build/replay.json'

formal: formal/formal_multicast_packet_fanout.sv $(RTL)
	mkdir -p build
	$(YOSYS) -Q -T -l build/formal.log -q -p 'read_verilog -formal -sv multicast_packet_fanout.sv formal/formal_multicast_packet_fanout.sv; hierarchy -check -top formal_multicast_packet_fanout; proc; flatten; memory_map; async2sync; chformal -lower; opt; sat -seq 9 -set-at 1 rst 1 -prove-asserts -verify'
	@grep 'SUCCESS' build/formal.log

formal-replay:
	mkdir -p build/replay
	cd formal && $(SBY) -f --prefix ../build/replay/formal replay.sby

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

waveform-policy: ../scripts/render_vcd_waveform.py
	$(PYTHON) -B run_fanout_policy.py --iverilog "$(IVERILOG)" --vvp "$(VVP)" \
		--example --wave build/policy/example/policy.vcd
	$(PYTHON) -B ../scripts/render_vcd_waveform.py build/policy/example/policy.vcd docs/policy_waveform.svg \
		--start 34 --end 190 --title 'Day 15 · A required copy continues while a recorder holds credit' \
		--signal 'tb_multicast_policy_fanout.clk=Clock' \
		--signal 'tb_multicast_policy_fanout.s_tag=Ingress tag' \
		--signal 'tb_multicast_policy_fanout.report_valid=Report valid' \
		--signal 'tb_multicast_policy_fanout.report_tag=Report tag' \
		--signal 'tb_multicast_policy_fanout.report_status=Report status' \
		--signal 'tb_multicast_policy_fanout.report_epoch=Policy epoch' \
		--signal 'tb_multicast_policy_fanout.report_enqueued=Copies committed' \
		--signal 'tb_multicast_policy_fanout.report_no_credit=No-credit mask' \
		--signal 'tb_multicast_policy_fanout.m_valid=Output valid mask' \
		--signal 'tb_multicast_policy_fanout.m_ready=Output ready mask'

waveform-replay: ../scripts/render_vcd_waveform.py
	$(PYTHON) -B replay_verify.py --example --iverilog "$(IVERILOG)" --vvp "$(VVP)" \
		--wave build/replay/example/replay.vcd
	$(PYTHON) -B ../scripts/render_vcd_waveform.py build/replay/example/replay.vcd docs/replay_waveform.svg \
		--start 48 --end 580 --title 'Day 15 · A pinned frame survives eviction, followed by exact gaps' \
		--signal 'tb_multicast_replay_buffer.next_sequence=Next journal sequence' \
		--signal 'tb_multicast_replay_buffer.earliest_sequence=Earliest retained sequence' \
		--signal 'tb_multicast_replay_buffer.resident_slots=Physical slots in use' \
		--signal 'tb_multicast_replay_buffer.m_valid=Output valid mask' \
		--signal 'tb_multicast_replay_buffer.m_ready=Output ready mask' \
		--signal 'tb_multicast_replay_buffer.m_gap=Gap notice mask' \
		--signal 'tb_multicast_replay_buffer.p0_seq=Healthy port sequence' \
		--signal 'tb_multicast_replay_buffer.p1_seq=Lagging port sequence' \
		--signal 'tb_multicast_replay_buffer.p1_resume=Lagging port resume' \
		--signal 'tb_multicast_replay_buffer.m_last=Last event mask'

clean:
	rm -rf build obj_dir
