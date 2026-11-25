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

lint:
	$(VERILATOR) --lint-only --Wall --top-module redundant_market_data -f rtl.f
	$(VERILATOR) --lint-only --Wall --top-module multisession_market_data -f rtl.f -f multisession.f
	$(VERILATOR) --lint-only --Wall --top-module mold_reorder -GMAX_REQUESTS_PER_HEAD=4 -GRETRY_BACKOFF_SHIFT=2 mold_reorder.sv

synth: synth-multisession synth-retry-policy
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

formal:
	mkdir -p $(BUILD)
	cd formal && $(SBY) -f --prefix ../$(BUILD)/session session.sby

clean:
	rm -rf $(BUILD) __pycache__

.PHONY: waveform waveform-multisession waveform-retry-policy

waveform: build/market_data.vvp ../scripts/render_vcd_waveform.py waveform-multisession waveform-retry-policy
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
