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
