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

.PHONY: all test test-default test-multisession test-recovery-loop icarus regress lint synth synth-multisession formal clean
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

lint:
	$(VERILATOR) --lint-only --Wall --top-module redundant_market_data -f rtl.f
	$(VERILATOR) --lint-only --Wall --top-module multisession_market_data -f rtl.f -f multisession.f

synth: synth-multisession
	mkdir -p $(BUILD)
	$(YOSYS) -ql $(BUILD)/synth.log -p 'read_verilog -sv $(RTL); synth -top redundant_market_data -noabc; check -assert; stat; write_json $(BUILD)/market_data.json'
	tail -n 24 $(BUILD)/synth.log

synth-multisession:
	mkdir -p $(BUILD)
	$(YOSYS) -ql $(BUILD)/multisession-synth.log -m slang -p 'read_slang --top multisession_market_data $(RTL) $(MULTI_RTL); synth -top multisession_market_data -noabc; check -assert; stat'
	tail -n 24 $(BUILD)/multisession-synth.log

formal:
	mkdir -p $(BUILD)
	cd formal && $(SBY) -f --prefix ../$(BUILD)/session session.sby

clean:
	rm -rf $(BUILD) __pycache__

.PHONY: waveform waveform-multisession

waveform: build/market_data.vvp ../scripts/render_vcd_waveform.py waveform-multisession
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
