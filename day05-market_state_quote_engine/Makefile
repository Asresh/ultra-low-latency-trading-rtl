SHELL := /bin/sh

PYTHON ?= python3
IVERILOG ?= iverilog
VVP ?= vvp
YOSYS ?= yosys
VERILATOR ?= verilator
NEXTPNR ?= nextpnr-ecp5
SBY ?= sby
BUILD ?= build
SEED ?= 7201
PROFILE ?= default
ITERATIONS ?= 5000

TOP := market_state_quote_engine
TB := tb_market_state_quote_engine
RTL := $(shell cat rtl.f)
PROFILE_FLAGS := $(shell $(PYTHON) -B verify_market.py params $(PROFILE))

.PHONY: all test replay-demo differential unit regress lint synth route report formal clean
all: test

test: unit differential replay-demo

unit: $(BUILD)/units/quote_publication.vvp $(BUILD)/units/quote_policy.vvp $(BUILD)/units/mbo_book.vvp
	$(VVP) $(BUILD)/units/quote_publication.vvp
	$(VVP) $(BUILD)/units/quote_policy.vvp
	$(VVP) $(BUILD)/units/mbo_book.vvp
	$(PYTHON) -B -m unittest discover -p 'test_market*.py'

differential: $(BUILD)/$(PROFILE)/engine.vvp
	$(PYTHON) -B verify_market.py run --profile $(PROFILE) --seed $(SEED) --iterations $(ITERATIONS) \
		--vvp "$(VVP)" --sim $< --build $(BUILD)/$(PROFILE)

regress:
	$(MAKE) differential PROFILE=default SEED=7201 ITERATIONS=5000
	$(MAKE) differential PROFILE=asym SEED=38177 ITERATIONS=5000
	$(MAKE) differential PROFILE=edge SEED=20283 ITERATIONS=3500
	$(MAKE) differential PROFILE=size_off SEED=88 ITERATIONS=2500

$(BUILD)/$(PROFILE)/engine.vvp: $(RTL) $(TB).sv market_codes.svh rtl.f market_reference.py Makefile
	mkdir -p $(dir $@)
	$(IVERILOG) -g2012 -Wall -s $(TB) $(PROFILE_FLAGS) -o $@ -f rtl.f $(TB).sv

$(BUILD)/units/quote_publication.vvp: quote_publication.sv tb_quote_publication.sv
	mkdir -p $(dir $@)
	$(IVERILOG) -g2012 -Wall -s tb_quote_publication -o $@ $^

$(BUILD)/units/quote_policy.vvp: passive_quote_policy.sv tb_passive_quote_policy.sv market_codes.svh
	mkdir -p $(dir $@)
	$(IVERILOG) -g2012 -Wall -s tb_passive_quote_policy -o $@ passive_quote_policy.sv tb_passive_quote_policy.sv

$(BUILD)/units/mbo_book.vvp: market_best_price.sv market_mbo_book.sv tb_market_mbo_book.sv market_codes.svh
	mkdir -p $(dir $@)
	$(IVERILOG) -g2012 -Wall -s tb_market_mbo_book -o $@ $(filter %.sv,$^)

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
	$(PYTHON) -B verify_market.py report $(BUILD)/$(PROFILE)/metrics.json

replay-demo:
	mkdir -p $(BUILD)/replay
	$(PYTHON) -B market_replay.py fixture $(BUILD)/replay/feed.csv
	$(PYTHON) -B market_replay.py replay $(BUILD)/replay/feed.csv \
		--out $(BUILD)/replay/observation.jsonl --inventory $(BUILD)/replay/inventory.json
	$(PYTHON) -B market_trace_tools.py audit $(BUILD)/replay/observation.jsonl --out $(BUILD)/replay/audit.json
	$(PYTHON) -B market_cosim.py run $(BUILD)/replay/observation.jsonl --build $(BUILD)/replay/cosim \
		--iverilog "$(IVERILOG)" --vvp "$(VVP)"
	$(PYTHON) -B market_checkpoint.py plan $(BUILD)/replay/inventory.json --anchor 0=65535 --anchor 1=70 \
		--feed $(BUILD)/replay/restore.csv --manifest $(BUILD)/replay/restore.json
	$(PYTHON) -B market_checkpoint.py verify $(BUILD)/replay/restore.json $(BUILD)/replay/restore.csv
	$(PYTHON) -B market_replay.py replay $(BUILD)/replay/restore.csv --out $(BUILD)/replay/restore.jsonl
	$(PYTHON) -B market_cosim.py run $(BUILD)/replay/restore.jsonl --build $(BUILD)/replay/restore-cosim \
		--iverilog "$(IVERILOG)" --vvp "$(VVP)"

formal:
	mkdir -p $(BUILD)/formal
	cd formal && $(SBY) -f -d "$(abspath $(BUILD)/formal/policy_prove)" policy.sby prove
	cd formal && $(SBY) -f -d "$(abspath $(BUILD)/formal/policy_cover)" policy.sby cover
	cd formal && $(SBY) -f -d "$(abspath $(BUILD)/formal/publication_prove)" publication.sby prove
	cd formal && $(SBY) -f -d "$(abspath $(BUILD)/formal/publication_cover)" publication.sby cover

clean:
	rm -rf $(BUILD)
