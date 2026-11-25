IVERILOG ?= iverilog
VVP ?= vvp
VERILATOR ?= verilator
YOSYS ?= yosys
SBY ?= sby
PYTHON ?= python3
SEED ?= 7201
BUILD := build
RTL := $(shell cat rtl.f)
MULTI_RTL := $(shell cat multisession.f)
GUARD_RTL := $(shell cat recovery_guard.f)
GUARD_AUX := $(filter-out mold_pair_ram.sv,$(GUARD_RTL))
GUARD_HEADER := mold_recovery_codes.svh
GUARD_TB := tb_mold_recovery_guard
SNAPSHOT_RTL := $(shell cat snapshot.f)
SNAPSHOT_TB := tb_mold_snapshot_cutover
SNAPSHOT_SCOPE := tb_snapshot_receiver_demo

.PHONY: all test test-default test-multisession test-recovery-loop test-retry-policy icarus regress lint synth synth-multisession synth-retry-policy formal clean
all: test

test: $(BUILD)/market_data.vvp
	$(PYTHON) scenarios.py --output $(BUILD) --seed $(SEED)
	$(VVP) $< +vectors=$(BUILD)

$(BUILD)/market_data.vvp: $(RTL) rtl.f tb_redundant_market_data.sv
	mkdir -p $(BUILD)
	$(IVERILOG) -g2012 -Wall -s tb_redundant_market_data -o $@ -f rtl.f tb_redundant_market_data.sv

test-default: $(BUILD)/market_data_default.vvp
	$(PYTHON) scenarios.py --output $(BUILD)/default --seed $(SEED) --default-geometry
	$(VVP) $< +vectors=$(BUILD)/default

$(BUILD)/market_data_default.vvp: $(RTL) rtl.f tb_redundant_market_data.sv
	mkdir -p $(BUILD)
	$(IVERILOG) -DDEFAULT_GEOMETRY -g2012 -Wall -s tb_redundant_market_data -o $@ -f rtl.f tb_redundant_market_data.sv

icarus: test

regress:
	$(MAKE) test SEED=7201
	$(MAKE) test SEED=39119
	$(MAKE) test-default SEED=7201
	$(PYTHON) -m unittest -v test_pcap_replay test_mold_recovery
	$(MAKE) test-multisession SEED=98741
	$(MAKE) test-recovery-loop
	$(MAKE) test-retry-policy
	$(MAKE) test-recovery-guard
	$(MAKE) test-snapshot

$(BUILD)/multisession.vvp: $(RTL) $(MULTI_RTL) rtl.f multisession.f tb_multisession_market_data.sv
	mkdir -p $(BUILD)
	$(IVERILOG) -g2012 -Wall -s tb_multisession_market_data -o $@ -f rtl.f -f multisession.f tb_multisession_market_data.sv

$(BUILD)/multisession-%.txt: session_vectors.py wire_codec.py
	$(PYTHON) -B session_vectors.py $@ --seed $*

test-multisession: $(BUILD)/multisession.vvp $(BUILD)/multisession-$(SEED).txt
	$(VVP) $< +vectors=$(BUILD)/multisession-$(SEED).txt

test-recovery-loop: $(BUILD)/multisession.vvp $(BUILD)/multisession-73457.txt
	$(VVP) $< +vectors=$(BUILD)/multisession-73457.txt +requests=$(BUILD)/recovery-requests.txt
	$(PYTHON) -B session_repair_example.py $(BUILD)/recovery-requests.txt $(BUILD)/recovery-beats.txt
	$(VVP) $< +vectors=$(BUILD)/multisession-73457.txt +repair=$(BUILD)/recovery-beats.txt

$(BUILD)/retry-scheduler.vvp: mold_reorder.sv tb_mold_reorder_retry.sv
	mkdir -p $(BUILD)
	$(IVERILOG) -g2012 -Wall -s tb_mold_reorder_retry -o $@ $^

$(BUILD)/multisession-bounded.vvp: $(RTL) $(MULTI_RTL) rtl.f multisession.f tb_multisession_market_data.sv
	mkdir -p $(BUILD)
	$(IVERILOG) -g2012 -Wall -s tb_multisession_market_data -Ptb_multisession_market_data.BOUNDED=1 -o $@ -f rtl.f -f multisession.f tb_multisession_market_data.sv

test-retry-policy: $(BUILD)/retry-scheduler.vvp $(BUILD)/multisession-bounded.vvp $(BUILD)/multisession-98741.txt
	$(VVP) $(BUILD)/retry-scheduler.vvp
	$(VVP) $(BUILD)/multisession-bounded.vvp +vectors=$(BUILD)/multisession-98741.txt

lint: lint-recovery-guard lint-snapshot
	$(VERILATOR) --lint-only --Wall --top-module redundant_market_data -f rtl.f
	$(VERILATOR) --lint-only --Wall --top-module multisession_market_data -f rtl.f -f multisession.f
	$(VERILATOR) --lint-only --Wall --top-module mold_reorder -GMAX_REQUESTS_PER_HEAD=4 -GRETRY_BACKOFF_SHIFT=2 mold_reorder.sv

synth: synth-multisession synth-retry-policy synth-recovery-guard synth-snapshot
	mkdir -p $(BUILD)
	$(YOSYS) -ql $(BUILD)/synth.log -p 'read_verilog -sv $(RTL); synth -top redundant_market_data -noabc; check -assert; stat; write_json $(BUILD)/market_data.json'
	tail -n 24 $(BUILD)/synth.log

synth-multisession:
	mkdir -p $(BUILD)
	$(YOSYS) -ql $(BUILD)/multisession-synth.log -m slang -p 'read_slang --top multisession_market_data $(RTL) $(MULTI_RTL); synth -top multisession_market_data -noabc; check -assert; stat'
	tail -n 24 $(BUILD)/multisession-synth.log

synth-retry-policy:
	mkdir -p $(BUILD)
	$(YOSYS) -ql $(BUILD)/retry-policy-synth.log -m slang -p 'read_slang --top mold_reorder -GGAP_TICKS=4 -GMAX_REQUESTS_PER_HEAD=4 -GRETRY_BACKOFF_SHIFT=2 mold_reorder.sv; synth -top mold_reorder -noabc; check -assert; stat'

formal: formal-recovery-guard formal-snapshot
	mkdir -p $(BUILD)
	cd formal && $(SBY) -f --prefix ../$(BUILD)/session session.sby

clean:
	rm -rf $(BUILD) __pycache__

.PHONY: waveform waveform-multisession waveform-retry-policy waveform-recovery-guard

waveform: build/market_data.vvp ../scripts/render_vcd_waveform.py waveform-multisession waveform-retry-policy waveform-recovery-guard waveform-snapshot
	$(PYTHON) -B scenarios.py --output $(BUILD)/waveform --seed 7201
	$(VVP) $< +vectors=$(BUILD)/waveform +waveform=$(BUILD)/redundant_market_data.vcd
	mkdir -p docs
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/redundant_market_data.vcd docs/waveform.svg \
		--start 45 --end 280 --title 'Day 01 · Duplicate packets produce one event' \
		--signal 'tb_redundant_market_data.clk=Clock' \
		--signal 'tb_redundant_market_data.feed_valid=Feed valid A/B' \
		--signal 'tb_redundant_market_data.feed_ready=Feed ready A/B' \
		--signal 'tb_redundant_market_data.feed_last=Feed last A/B' \
		--signal 'tb_redundant_market_data.expected_sequence=Next sequence' \
		--signal 'tb_redundant_market_data.event_valid=Event valid' \
		--signal 'tb_redundant_market_data.event_ready=Event ready'

waveform-multisession: $(BUILD)/multisession.vvp $(BUILD)/multisession-73457.txt ../scripts/render_vcd_waveform.py
	$(VVP) $< +vectors=$(BUILD)/multisession-73457.txt +waveform=$(BUILD)/multisession-market-data.vcd
	mkdir -p docs
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/multisession-market-data.vcd docs/multisession.svg \
		--start 1320 --end 1860 --title 'Day 01 · An epoch fence waits for a held event' \
		--signal 'tb_multisession_market_data.clk=Clock' \
		--signal 'tb_multisession_market_data.cfg_valid=Install command' \
		--signal 'tb_multisession_market_data.monitor_fenced=Slot 0 fenced' \
		--signal 'tb_multisession_market_data.event_valid=Event valid' \
		--signal 'tb_multisession_market_data.event_ready=Event ready' \
		--signal 'tb_multisession_market_data.event_slot=Event slot' \
		--signal 'tb_multisession_market_data.event_epoch=Event epoch' \
		--signal 'tb_multisession_market_data.ack_valid=Acknowledgment valid' \
		--signal 'tb_multisession_market_data.ack_epoch=Installed epoch' \
		--signal 'tb_multisession_market_data.ack_ready=Acknowledgment ready'

waveform-retry-policy: $(BUILD)/retry-scheduler.vvp ../scripts/render_vcd_waveform.py
	$(VVP) $< +waveform=$(BUILD)/retry-policy.vcd
	mkdir -p docs
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/retry-policy.vcd docs/retry-policy.svg \
		--start 20 --end 950 --title 'Day 01 · Bounded retries resume on head progress' \
		--signal 'tb_mold_reorder_retry.s_valid=Incoming Observation' \
		--signal 'tb_mold_reorder_retry.gap_pending=Unresolved Gap' \
		--signal 'tb_mold_reorder_retry.request_valid=Recovery Request' \
		--signal 'tb_mold_reorder_retry.dut.retry_policy.attempts=Current Head Attempts' \
		--signal 'tb_mold_reorder_retry.m_valid=Passive Repair Available' \
		--signal 'tb_mold_reorder_retry.expected_sequence=Current Head Sequence'

.PHONY: test-recovery-guard test-guarded-recovery-loop lint-recovery-guard synth-recovery-guard formal-recovery-guard waveform-recovery-guard

$(BUILD)/recovery-guard.vvp: $(GUARD_TB).sv $(GUARD_RTL) $(GUARD_HEADER) recovery_guard.f
	mkdir -p $(BUILD)
	$(IVERILOG) -g2012 -Wall -s $(GUARD_TB) -o $@ -f recovery_guard.f $<

$(BUILD)/recovery-guard-default.vvp: $(GUARD_TB).sv $(GUARD_RTL) $(GUARD_HEADER) recovery_guard.f
	mkdir -p $(BUILD)
	$(IVERILOG) -g2012 -Wall -s $(GUARD_TB) -P$(GUARD_TB).PACKET_BYTES=256 \
		-P$(GUARD_TB).MESSAGE_BYTES=64 -P$(GUARD_TB).BLOCKS=16 -P$(GUARD_TB).WINDOW=16 \
		-P$(GUARD_TB).EPOCH_W=16 -P$(GUARD_TB).TOKEN_W=16 -o $@ -f recovery_guard.f $<

$(BUILD)/recovery-dispatch.vvp: tb_mold_recovery_dispatch.sv mold_recovery_dispatch.sv $(GUARD_HEADER)
	mkdir -p $(BUILD)
	$(IVERILOG) -g2012 -Wall -s tb_mold_recovery_dispatch -o $@ $(filter %.sv,$^)

$(BUILD)/guarded-repair.vvp: tb_multisession_market_data.sv tb_guarded_repair_driver.sv $(RTL) $(MULTI_RTL) $(GUARD_AUX) $(GUARD_HEADER) rtl.f multisession.f recovery_guard.f
	mkdir -p $(BUILD)
	$(IVERILOG) -g2012 -Wall -s tb_multisession_market_data -Ptb_multisession_market_data.GUARDED=1 \
		-o $@ -f rtl.f -f multisession.f $(GUARD_AUX) tb_multisession_market_data.sv tb_guarded_repair_driver.sv

test-guarded-recovery-loop: $(BUILD)/guarded-repair.vvp $(BUILD)/multisession.vvp $(BUILD)/multisession-73457.txt
	$(VVP) $(BUILD)/multisession.vvp +vectors=$(BUILD)/multisession-73457.txt +requests=$(BUILD)/guard-requests.txt
	$(PYTHON) -B guarded_repair_fixture.py $(BUILD)/guard-requests.txt $(BUILD)/guarded-repair-loop.txt
	$(VVP) $< +vectors=$(BUILD)/multisession-73457.txt +guarded_repair=$(BUILD)/guarded-repair-loop.txt

test-recovery-guard: $(BUILD)/recovery-guard.vvp $(BUILD)/recovery-guard-default.vvp $(BUILD)/recovery-dispatch.vvp test-guarded-recovery-loop
	$(PYTHON) -B recovery_guard_vectors.py $(BUILD)/recovery-guard.txt
	$(VVP) $< +vectors=$(BUILD)/recovery-guard.txt
	$(PYTHON) -B recovery_guard_vectors.py $(BUILD)/recovery-guard-default.txt --default --seed 61227
	$(VVP) $(BUILD)/recovery-guard-default.vvp +vectors=$(BUILD)/recovery-guard-default.txt
	$(VVP) $(BUILD)/recovery-dispatch.vvp

lint-recovery-guard:
	$(VERILATOR) --lint-only --Wall --top-module mold_recovery_guard -f recovery_guard.f
	$(VERILATOR) --lint-only --Wall --top-module mold_recovery_dispatch -f recovery_guard.f
	$(VERILATOR) --lint-only --Wall --top-module mold_repair_lane_mux -f recovery_guard.f

synth-recovery-guard:
	mkdir -p $(BUILD)
	@set -e; for top in mold_recovery_guard mold_recovery_dispatch mold_repair_lane_mux; do \
		$(YOSYS) -ql $(BUILD)/$$top-synth.log -m slang -p "read_slang --top $$top $(GUARD_RTL); synth -top $$top -noabc; check -assert; stat"; \
	done

formal-recovery-guard:
	mkdir -p $(BUILD)
	cd formal && $(SBY) -f --prefix ../$(BUILD)/formal-lease recovery_leases.sby

waveform-recovery-guard: $(BUILD)/recovery-guard.vvp ../scripts/render_vcd_waveform.py
	$(PYTHON) -B recovery_guard_vectors.py $(BUILD)/recovery-guard-wave.txt --cycles 0
	$(VVP) $< +vectors=$(BUILD)/recovery-guard-wave.txt +waves=$(BUILD)/recovery-guard.vcd
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/recovery-guard.vcd docs/recovery-guard.svg \
		--start 1740 --end 2800 --title 'Day 01 · Recovery lease checks and replay fence' \
		--signal '$(GUARD_TB).audit_status=Audit Verdict' \
		--signal '$(GUARD_TB).audit_valid=Audit Valid' \
		--signal '$(GUARD_TB).audit_ready=Audit Ready' \
		--signal '$(GUARD_TB).audit_coverage=Admitted Coverage' \
		--signal '$(GUARD_TB).replay_valid=Replay Valid' \
		--signal '$(GUARD_TB).replay_ready=Replay Ready' \
		--signal '$(GUARD_TB).dut.leases.replay_fence=Same-slot Epoch Fence'

.PHONY: test-snapshot test-snapshot-demo lint-snapshot synth-snapshot formal-snapshot waveform-snapshot

$(BUILD)/snapshot-cutover.vvp: $(SNAPSHOT_TB).sv $(SNAPSHOT_RTL) snapshot.f
	mkdir -p $(BUILD)
	$(IVERILOG) -g2012 -s $(SNAPSHOT_TB) -o $@ -f snapshot.f $<

$(BUILD)/snapshot-single.vvp: $(SNAPSHOT_TB).sv $(SNAPSHOT_RTL) snapshot.f
	mkdir -p $(BUILD)
	$(IVERILOG) -g2012 -s $(SNAPSHOT_TB) -P$(SNAPSHOT_TB).DELTAS=1 -o $@ -f snapshot.f $<

$(BUILD)/snapshot-default.vvp: $(SNAPSHOT_TB).sv $(SNAPSHOT_RTL) snapshot.f
	mkdir -p $(BUILD)
	$(IVERILOG) -g2012 -s $(SNAPSHOT_TB) -P$(SNAPSHOT_TB).EPOCH_W=16 -P$(SNAPSHOT_TB).WAIT_W=16 \
		-P$(SNAPSHOT_TB).DELTAS=8 -P$(SNAPSHOT_TB).MAX_ROWS=16 -o $@ -f snapshot.f $<

$(BUILD)/snapshot-demo.vvp: $(SNAPSHOT_SCOPE).sv $(RTL) $(MULTI_RTL) $(SNAPSHOT_RTL) rtl.f multisession.f snapshot.f
	mkdir -p $(BUILD)
	$(IVERILOG) -g2012 -s $(SNAPSHOT_SCOPE) -o $@ -f rtl.f -f multisession.f -f snapshot.f $<

test-snapshot: $(BUILD)/snapshot-cutover.vvp $(BUILD)/snapshot-single.vvp $(BUILD)/snapshot-default.vvp test-snapshot-demo
	$(PYTHON) -B -m unittest test_snapshot_image
	$(PYTHON) -B gen_snapshot_cutover_vectors.py $(BUILD)/snapshot-cutover.txt
	$(VVP) $< +vectors=$(BUILD)/snapshot-cutover.txt
	$(PYTHON) -B gen_snapshot_cutover_vectors.py $(BUILD)/snapshot-single.txt --depth 1 --seed 7923
	$(VVP) $(BUILD)/snapshot-single.vvp +vectors=$(BUILD)/snapshot-single.txt
	$(PYTHON) -B gen_snapshot_cutover_vectors.py $(BUILD)/snapshot-default.txt --default --seed 81193
	$(VVP) $(BUILD)/snapshot-default.vvp +vectors=$(BUILD)/snapshot-default.txt

test-snapshot-demo: $(BUILD)/snapshot-demo.vvp
	$(PYTHON) -B snapshot_cutover_demo.py build $(BUILD)/snapshot-demo
	$(VVP) $< +out=$(BUILD)/snapshot-demo
	$(PYTHON) -B snapshot_cutover_demo.py check $(BUILD)/snapshot-demo

lint-snapshot:
	$(VERILATOR) --lint-only -Wall --top-module mold_snapshot_cutover -f snapshot.f

synth-snapshot:
	mkdir -p $(BUILD)
	$(YOSYS) -ql $(BUILD)/snapshot-synth.log -m slang -p 'read_slang --top mold_snapshot_cutover $(SNAPSHOT_RTL); synth -top mold_snapshot_cutover -noabc; check -assert; stat'

formal-snapshot:
	mkdir -p $(BUILD)
	cd formal && $(SBY) -f --prefix ../$(BUILD)/formal-snapshot snapshot_cutover.sby

waveform-snapshot: $(BUILD)/snapshot-demo.vvp
	$(PYTHON) -B snapshot_cutover_demo.py build $(BUILD)/snapshot-wave
	$(VVP) $< +out=$(BUILD)/snapshot-wave +waves=$(BUILD)/snapshot-wave/native.vcd
	$(PYTHON) -B snapshot_cutover_demo.py check $(BUILD)/snapshot-wave
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/snapshot-wave/native.vcd docs/snapshot-cutover.svg \
		--start 460 --end 1920 --title 'Snapshot cutover drains the old offer and gates new deltas' \
		--signal '$(SNAPSHOT_SCOPE).phase=Cutover Phase' \
		--signal '$(SNAPSHOT_SCOPE).m_valid=Live Valid' --signal '$(SNAPSHOT_SCOPE).m_ready=Live Ready' \
		--signal '$(SNAPSHOT_SCOPE).m_slot=Live Slot' --signal '$(SNAPSHOT_SCOPE).m_epoch=Live Epoch' \
		--signal '$(SNAPSHOT_SCOPE).cfg_valid=Native Command' --signal '$(SNAPSHOT_SCOPE).ack_valid=Native Reply' \
		--signal '$(SNAPSHOT_SCOPE).restore_valid=Book Commit Offer' --signal '$(SNAPSHOT_SCOPE).restore_ready=Book Ready' \
		--signal '$(SNAPSHOT_SCOPE).delta_used=Buffered Deltas' --signal '$(SNAPSHOT_SCOPE).output_sequence=Live Sequence'
