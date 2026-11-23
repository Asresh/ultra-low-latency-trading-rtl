SHELL := /bin/sh

PYTHON ?= python3
IVERILOG ?= iverilog
VVP ?= vvp
VERILATOR ?= verilator
YOSYS ?= yosys
NEXTPNR ?= nextpnr-ecp5
SEED ?= 7201
COMMANDS ?= 4000
BUILD ?= build
RTL := $(shell cat rtl.f)
TOP := banked_matching_engine
TB := tb_banked_matching_engine
PROFILE ?= default

.PHONY: all test unit regress lint synth route report clean
all: test

test: unit $(BUILD)/$(PROFILE)/engine.vvp
	$(PYTHON) -B verify_matching.py run --profile $(PROFILE) --seed $(SEED) --commands $(COMMANDS) \
		--vvp "$(VVP)" --sim $(BUILD)/$(PROFILE)/engine.vvp --build $(BUILD)/$(PROFILE)

regress:
	$(MAKE) test PROFILE=default SEED=7201
	$(MAKE) test PROFILE=default SEED=39119
	$(MAKE) test PROFILE=wide SEED=9109
	$(MAKE) test PROFILE=tiny SEED=9887

unit: $(BUILD)/transport.vvp
	$(VVP) $<
	$(PYTHON) -B -m unittest -v test_matching_model

PARAMS_default := -P $(TB).BANKS=3 -P $(TB).PRICE_LEVELS=12 -P $(TB).ORDER_SLOTS=12 \
	-P $(TB).QTY_W=16 -P $(TB).CMD_DEPTH=4 -P $(TB).EVENT_DEPTH=4
PARAMS_wide := -P $(TB).BANKS=4 -P $(TB).PRICE_LEVELS=7 -P $(TB).ORDER_SLOTS=9 \
	-P $(TB).QTY_W=12 -P $(TB).CMD_DEPTH=3 -P $(TB).EVENT_DEPTH=2
PARAMS_tiny := -P $(TB).BANKS=1 -P $(TB).PRICE_LEVELS=1 -P $(TB).ORDER_SLOTS=2 \
	-P $(TB).QTY_W=4 -P $(TB).CMD_DEPTH=1 -P $(TB).EVENT_DEPTH=1

$(BUILD)/$(PROFILE)/engine.vvp: $(RTL) matching_codes.svh $(TB).sv rtl.f Makefile
	mkdir -p $(dir $@)
	$(IVERILOG) -g2012 -Wall -s $(TB) $(PARAMS_$(PROFILE)) -o $@ -f rtl.f $(TB).sv

$(BUILD)/transport.vvp: matching_fifo.sv matching_arbiter.sv tb_matching_transport.sv
	mkdir -p $(dir $@)
	$(IVERILOG) -g2012 -Wall -s tb_matching_transport -o $@ $^

lint:
	$(VERILATOR) --lint-only -Wall --top-module $(TOP) -f rtl.f

synth:
	mkdir -p $(BUILD)
	$(YOSYS) -Q -l $(BUILD)/synth.log -p 'read_verilog -sv $(RTL); synth -top $(TOP); check -assert; stat; write_json $(BUILD)/$(TOP).json'

route:
	mkdir -p $(BUILD)
	$(YOSYS) -ql $(BUILD)/ecp5-synth.log -p 'read_verilog -sv $(RTL); synth_ecp5 -top $(TOP) -json $(BUILD)/ecp5-mapped.json; check -assert'
	$(NEXTPNR) --85k --package CABGA381 --speed 8 --out-of-context --threads 1 --seed 1 \
		--freq 100 --timing-allow-fail --json $(BUILD)/ecp5-mapped.json \
		--report $(BUILD)/ecp5-timing.json --log $(BUILD)/ecp5-route.log -q

report:
	$(PYTHON) -B verify_matching.py report $(BUILD)/$(PROFILE)/metrics.json

clean:
	rm -rf $(BUILD)
