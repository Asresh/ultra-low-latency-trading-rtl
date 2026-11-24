IVERILOG ?= iverilog
VVP ?= vvp
VERILATOR ?= verilator
YOSYS ?= yosys
PYTHON ?= python3
SEED ?= 7201
BUILD := build
RTL := $(shell cat rtl.f)

.PHONY: all test test-default icarus regress lint synth clean
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
	$(PYTHON) -m unittest -v test_pcap_replay

lint:
	$(VERILATOR) --lint-only --Wall --top-module redundant_market_data -f rtl.f

synth:
	mkdir -p $(BUILD)
	$(YOSYS) -ql $(BUILD)/synth.log -p 'read_verilog -sv $(RTL); synth -top redundant_market_data -noabc; check -assert; stat; write_json $(BUILD)/market_data.json'
	tail -n 24 $(BUILD)/synth.log

clean:
	rm -rf $(BUILD) __pycache__

.PHONY: waveform

waveform: build/market_data.vvp ../scripts/render_vcd_waveform.py
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
