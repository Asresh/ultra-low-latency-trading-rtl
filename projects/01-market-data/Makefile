PYTHON ?= python3
IVERILOG ?= iverilog
VVP ?= vvp
VERILATOR ?= verilator
YOSYS ?= yosys
SEED ?= 7201
BUILD := build

.PHONY: all test lint synth clean
all: test

test: $(BUILD)/itch_decode_tb
	$(PYTHON) tb/gen_vectors.py --directory $(BUILD) --seed $(SEED)
	$(VVP) $(BUILD)/itch_decode_tb +vectors=$(BUILD)

$(BUILD)/itch_decode_tb: rtl/itch_decode.sv tb/itch_decode_tb.sv
	mkdir -p $(BUILD)
	$(IVERILOG) -g2012 -Wall -s itch_decode_tb -o $@ $^

lint:
	$(VERILATOR) --lint-only --Wall --top-module itch_decode rtl/itch_decode.sv

synth:
	mkdir -p $(BUILD)
	$(YOSYS) -ql $(BUILD)/synth.log -p 'read_verilog -sv rtl/itch_decode.sv; synth -top itch_decode; check -assert; stat; write_json $(BUILD)/itch_decode.json'
	tail -n 26 $(BUILD)/synth.log

clean:
	rm -rf $(BUILD)
