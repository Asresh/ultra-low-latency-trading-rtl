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

test: unit differential replay-demo order-bridge-test inventory-test

unit: $(BUILD)/units/quote_publication.vvp $(BUILD)/units/quote_reacquire.vvp $(BUILD)/units/quote_policy.vvp $(BUILD)/units/mbo_book.vvp
	$(VVP) $(BUILD)/units/quote_publication.vvp
	$(VVP) $(BUILD)/units/quote_reacquire.vvp
	$(VVP) $(BUILD)/units/quote_policy.vvp
	$(VVP) $(BUILD)/units/mbo_book.vvp
	$(PYTHON) -B -m unittest discover -p 'test_market*.py'

differential: $(BUILD)/$(PROFILE)/engine.vvp
	$(PYTHON) -B verify_market.py run --profile $(PROFILE) --seed $(SEED) --iterations $(ITERATIONS) \
		--vvp "$(VVP)" --sim $< --build $(BUILD)/$(PROFILE)

regress: unit replay-demo order-bridge-test inventory-test
	$(MAKE) differential PROFILE=default SEED=7201 ITERATIONS=5000
	$(MAKE) differential PROFILE=asym SEED=38177 ITERATIONS=5000
	$(MAKE) differential PROFILE=edge SEED=20283 ITERATIONS=3500
	$(MAKE) differential PROFILE=size_off SEED=88 ITERATIONS=2500

$(BUILD)/$(PROFILE)/engine.vvp: $(RTL) $(TB).sv market_codes.svh rtl.f market_reference.py Makefile
	mkdir -p $(dir $@)
	$(IVERILOG) -g2012 -Wall -s $(TB) $(PROFILE_FLAGS) -o $@ -f rtl.f $(TB).sv

$(BUILD)/units/quote_publication.vvp: quote_publication.sv quote_publication_pipeline.sv tb_quote_publication.sv
	mkdir -p $(dir $@)
	$(IVERILOG) -g2012 -Wall -s tb_quote_publication -o $@ $^

$(BUILD)/units/quote_reacquire.vvp: quote_publication.sv quote_publication_pipeline.sv tb_quote_reacquire.sv
	mkdir -p $(dir $@)
	$(IVERILOG) -g2012 -Wall -s tb_quote_reacquire -o $@ $^

$(BUILD)/units/quote_policy.vvp: passive_quote_policy.sv tb_passive_quote_policy.sv market_codes.svh
	mkdir -p $(dir $@)
	$(IVERILOG) -g2012 -Wall -s tb_passive_quote_policy -o $@ passive_quote_policy.sv tb_passive_quote_policy.sv

$(BUILD)/units/mbo_book.vvp: market_best_price.sv market_mbo_book.sv tb_market_mbo_book.sv market_codes.svh
	mkdir -p $(dir $@)
	$(IVERILOG) -g2012 -Wall -s tb_market_mbo_book -o $@ $(filter %.sv,$^)

lint: order-bridge-lint inventory-lint
	$(VERILATOR) --lint-only -Wall --top-module $(TOP) -f rtl.f

synth: order-bridge-synth inventory-synth
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

.PHONY: waveform
waveform: $(BUILD)/units/quote_reacquire.vvp order-bridge-waveform inventory-waveform
	$(VVP) $< +wave=$(BUILD)/quote-reacquire.vcd
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/quote-reacquire.vcd waveform.svg \
		--start 25 --end 80 --title 'Quote intents · Stale transfer and revision on reacquisition' \
		--signal 'tb_quote_reacquire.clk=Clock' \
		--signal 'tb_quote_reacquire.q_valid=Quote valid' \
		--signal 'tb_quote_reacquire.q_ready=Quote ready' \
		--signal 'tb_quote_reacquire.q_current=Quote current' \
		--signal 'tb_quote_reacquire.q_instrument=Instrument' \
		--signal 'tb_quote_reacquire.q_revision=Revision' \
		--signal 'tb_quote_reacquire.q_size=Quote size' \
		--signal 'tb_quote_reacquire.q_bid=Quote bid'

.PHONY: order-bridge-test order-bridge-lint order-bridge-synth order-bridge-waveform

ORDER_BRIDGE := quote_order_bridge
ORDER_BRIDGE_TB := tb_quote_order_bridge

$(BUILD)/units/order_bridge.vvp: $(ORDER_BRIDGE).sv $(ORDER_BRIDGE_TB).sv
	mkdir -p $(dir $@)
	$(IVERILOG) -g2012 -Wall -s $(ORDER_BRIDGE_TB) -o $@ $^

order-bridge-test: $(BUILD)/units/order_bridge.vvp
	$(VVP) $<

order-bridge-lint:
	$(VERILATOR) --lint-only -Wall --top-module $(ORDER_BRIDGE) $(ORDER_BRIDGE).sv

order-bridge-synth:
	mkdir -p $(BUILD)
	$(YOSYS) -Q -l $(BUILD)/order-bridge-synth.log -p 'read_verilog -sv $(ORDER_BRIDGE).sv; synth -top $(ORDER_BRIDGE); check -assert; stat; write_json $(BUILD)/$(ORDER_BRIDGE).json'

order-bridge-waveform: $(BUILD)/units/order_bridge.vvp
	$(VVP) $< +wave=$(BUILD)/order-bridge.vcd
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/order-bridge.vcd order-bridge.svg \
		--start 30 --end 135 --title 'Quote order bridge · Superseded add under backpressure' \
		--signal 'tb_quote_order_bridge.clk=Clock' \
		--signal 'tb_quote_order_bridge.s_valid=Quote intent' \
		--signal 'tb_quote_order_bridge.s_bid=New bid' \
		--signal 'tb_quote_order_bridge.m_valid=Action valid' \
		--signal 'tb_quote_order_bridge.m_ready=Action ready' \
		--signal 'tb_quote_order_bridge.m_applicable=Applicable' \
		--signal 'tb_quote_order_bridge.m_ask=Ask side' \
		--signal 'tb_quote_order_bridge.m_price=Held action price'

INVENTORY_RTL := quote_inventory_manager.sv quote_inventory_ledger.sv quote_inventory_overlay.sv
INVENTORY_TB := tb_quote_inventory_manager
INVENTORY_SCOPE := $(INVENTORY_TB)
.PHONY: inventory-test inventory-lint inventory-synth inventory-formal inventory-waveform

define inventory_case
$(BUILD)/inventory-$(1).vvp: $(INVENTORY_TB).sv $(INVENTORY_RTL) quote_inventory_codes.svh
	mkdir -p $(BUILD)
	$(IVERILOG) -g2012 -I. -s $(INVENTORY_TB) -P$(INVENTORY_TB).INSTRUMENTS=$(2) \
		-P$(INVENTORY_TB).LEVELS=$(3) -o $$@ $$(filter %.sv,$$^)

$(BUILD)/inventory-$(1).hex: inventory_vectors.py inventory_reference.py
	mkdir -p $(BUILD)
	$(PYTHON) -B inventory_vectors.py $$@ --instruments $(2) --levels $(3) --seed $(4)

.PHONY: inventory-$(1)
inventory-$(1): $(BUILD)/inventory-$(1).vvp $(BUILD)/inventory-$(1).hex
	$(VVP) $$< +vectors=$(BUILD)/inventory-$(1).hex
endef

$(eval $(call inventory_case,main,3,31,54313))
$(eval $(call inventory_case,one,1,11,24425))
$(eval $(call inventory_case,wide,5,64,76039))

inventory-test: inventory-main inventory-one inventory-wide

inventory-lint:
	$(VERILATOR) --lint-only -Wall -I. --top-module quote_inventory_manager $(INVENTORY_RTL)

inventory-synth:
	mkdir -p $(BUILD)
	$(YOSYS) -ql $(BUILD)/inventory-synth.log -p 'read_verilog -sv -I. $(INVENTORY_RTL); synth -top quote_inventory_manager; check -assert; stat'

inventory-formal:
	cd formal && $(SBY) -f --prefix ../$(BUILD)/inventory-formal inventory.sby

inventory-waveform: $(BUILD)/inventory-main.vvp $(BUILD)/inventory-main.hex
	$(VVP) $< +vectors=$(BUILD)/inventory-main.hex +wave=$(BUILD)/inventory.vcd
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/inventory.vcd inventory-waveform.svg \
		--start 50 --end 305 --title 'Inventory quote · A fill invalidates a held passive quote' \
		--signal '$(INVENTORY_SCOPE).c_valid=Inventory command' \
		--signal '$(INVENTORY_SCOPE).r_position=Inventory reply position' \
		--signal '$(INVENTORY_SCOPE).m_valid=Quote offer valid' \
		--signal '$(INVENTORY_SCOPE).m_ready=Quote offer ready' \
		--signal '$(INVENTORY_SCOPE).m_current=Quote current' \
		--signal '$(INVENTORY_SCOPE).m_instrument=Instrument' \
		--signal '$(INVENTORY_SCOPE).m_bid_size=Offered bid size' \
		--signal '$(INVENTORY_SCOPE).m_ask_size=Offered ask size'
