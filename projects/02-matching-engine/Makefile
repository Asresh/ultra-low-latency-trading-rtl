SHELL := /bin/sh

IVERILOG ?= iverilog
VVP ?= vvp
YOSYS ?= yosys
BUILD ?= build

RTL := rtl/price_priority.sv rtl/price_time_matcher.sv
TB := tb/tb_price_time_matcher.sv
TOP := price_time_matcher
TB_TOP := tb_price_time_matcher

.PHONY: all test synth clean
all: test

test: $(BUILD)/matcher.vvp $(BUILD)/matcher_single.vvp $(BUILD)/matcher_default.vvp
	$(VVP) $(BUILD)/matcher.vvp
	$(VVP) $(BUILD)/matcher_single.vvp
	$(VVP) $(BUILD)/matcher_default.vvp

$(BUILD):
	mkdir -p $@

$(BUILD)/matcher.vvp: $(RTL) $(TB) | $(BUILD)
	$(IVERILOG) -g2012 -Wall -s $(TB_TOP) -o $@ $(RTL) $(TB)

$(BUILD)/matcher_single.vvp: $(RTL) $(TB) | $(BUILD)
	$(IVERILOG) -g2012 -Wall -s $(TB_TOP) \
		-P $(TB_TOP).PRICE_LEVELS=1 -P $(TB_TOP).ORDER_SLOTS=1 \
		-P $(TB_TOP).RANDOM_COMMANDS=200 -o $@ $(RTL) $(TB)

$(BUILD)/matcher_default.vvp: $(RTL) $(TB) | $(BUILD)
	$(IVERILOG) -g2012 -Wall -s $(TB_TOP) \
		-P $(TB_TOP).PRICE_LEVELS=16 -P $(TB_TOP).ORDER_SLOTS=16 \
		-P $(TB_TOP).QW=16 \
		-o $@ $(RTL) $(TB)

synth: | $(BUILD)
	$(YOSYS) -Q -l $(BUILD)/synth.log -p 'read_verilog -sv $(RTL); synth -top $(TOP); check -assert; stat; write_json $(BUILD)/matcher.json'

clean:
	rm -rf $(BUILD)
