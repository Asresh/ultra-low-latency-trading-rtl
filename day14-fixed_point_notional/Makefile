PYTHON ?= python3
IVERILOG ?= iverilog
VVP ?= vvp
VERILATOR ?= verilator
YOSYS ?= yosys
SBY ?= sby
BUILD := build
TOP := fixed_point_notional
RTL := $(shell cat rtl.f)
HEADERS := notional_codes.svh
PROFILES := 0 8 31

.PHONY: all test lint synth formal demo waveform clean
all: test

$(BUILD):
	mkdir -p $@

$(BUILD)/q%.vvp: tb_fixed_point_notional.sv $(RTL) $(HEADERS) rtl.f | $(BUILD)
	$(IVERILOG) -g2012 -I. -s tb_fixed_point_notional -Ptb_fixed_point_notional.PRICE_FRAC=$* -o $@ -f rtl.f $<

$(BUILD)/divide-small.vvp: tb_notional_divide.sv notional_divide.sv | $(BUILD)
	$(IVERILOG) -g2012 -s tb_notional_divide -Ptb_notional_divide.NUM_W=9 -Ptb_notional_divide.DIVISOR=13 -o $@ notional_divide.sv $<

$(BUILD)/divide-wide.vvp: tb_notional_divide.sv notional_divide.sv | $(BUILD)
	$(IVERILOG) -g2012 -s tb_notional_divide -o $@ notional_divide.sv $<

$(BUILD)/pair.vvp: tb_notional_pair.sv $(RTL) $(HEADERS) rtl.f | $(BUILD)
	$(IVERILOG) -g2012 -I. -s tb_notional_pair -o $@ -f rtl.f $<

test: $(BUILD)/pair.vvp $(foreach frac,$(PROFILES),$(BUILD)/q$(frac).vvp) $(BUILD)/divide-small.vvp $(BUILD)/divide-wide.vvp
	$(PYTHON) -B -m unittest discover -v
	$(VVP) $(BUILD)/divide-small.vvp
	$(VVP) $(BUILD)/divide-wide.vvp
	$(PYTHON) -B notional_workload.py $(BUILD)
	@set -e; for frac in $(PROFILES); do $(VVP) $(BUILD)/q$$frac.vvp +source=$(BUILD)/q$$frac.txt +trace=$(BUILD)/q$$frac.trace; done
	$(PYTHON) -B notional_capture.py $(BUILD)
	$(VVP) $(BUILD)/pair.vvp +source=$(BUILD)/q8.txt
	$(PYTHON) -B notional_pair_campaign.py --iverilog "$(IVERILOG)" --vvp "$(VVP)"
	$(MAKE) demo

demo: $(BUILD)/q8.vvp
	$(PYTHON) -B notional_batch.py fixtures/first_orders.csv $(BUILD)/lesson.csv $(BUILD)/lesson-from-csv.txt
	$(PYTHON) -B notional_wire.py pack fixtures/first_orders.csv $(BUILD)/lesson.nom1
	$(PYTHON) -B notional_wire.py tape $(BUILD)/lesson.nom1 $(BUILD)/lesson.txt
	cmp $(BUILD)/lesson-from-csv.txt $(BUILD)/lesson.txt
	$(VVP) $(BUILD)/q8.vvp +small +source=$(BUILD)/lesson.txt +trace=$(BUILD)/lesson.trace $(DEMO_WAVEFORM)
	$(PYTHON) -B notional_reconcile.py $(BUILD)/lesson.nom1 $(BUILD)/lesson.trace --output $(BUILD)/lesson-audit.json

WAVE_SCOPE := tb_fixed_point_notional
waveform: DEMO_WAVEFORM := +waveform=$(BUILD)/notional-demo.vcd
waveform: demo
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/notional-demo.vcd waveform.svg \
		--start 2130 --end 2250 --title "Rounded notional response waits for the consumer" \
		--signal '$(WAVE_SCOPE).clk=Clock' --signal '$(WAVE_SCOPE).req_ready=Request ready' \
		--signal '$(WAVE_SCOPE).req_valid=Request valid' --signal '$(WAVE_SCOPE).rsp_valid=Response valid' \
		--signal '$(WAVE_SCOPE).rsp_ready=Response ready' --signal '$(WAVE_SCOPE).rsp_tag=Response tag' \
		--signal '$(WAVE_SCOPE).rsp_gross=Gross' --signal '$(WAVE_SCOPE).rsp_fee=Fee' \
		--signal '$(WAVE_SCOPE).rsp_cash=Cash debit'

lint:
	$(VERILATOR) --lint-only -Wall -I. --top-module $(TOP) -f rtl.f
	$(VERILATOR) --lint-only -Wall -I. --top-module notional_pair -f rtl.f

synth: | $(BUILD)
	$(YOSYS) -Q -q -l $(BUILD)/synth.log -m slang -p 'read_slang -I. --top $(TOP) $(RTL); synth -top $(TOP) -noabc; check -assert; stat'
	$(YOSYS) -Q -q -l $(BUILD)/pair-synth.log -m slang -p 'read_slang -I. --top notional_pair $(RTL); synth -top notional_pair -noabc; check -assert; stat'
	tail -n 20 $(BUILD)/synth.log
	tail -n 20 $(BUILD)/pair-synth.log

formal: | $(BUILD)
	cd formal && $(SBY) -f --prefix ../$(BUILD)/division divide.sby prove cover

clean:
	rm -rf $(BUILD)
