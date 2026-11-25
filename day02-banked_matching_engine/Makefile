SHELL := /bin/sh

PYTHON ?= python3
IVERILOG ?= iverilog
VVP ?= vvp
VERILATOR ?= verilator
YOSYS ?= yosys
NEXTPNR ?= nextpnr-ecp5
SBY ?= sby
SEED ?= 7201
COMMANDS ?= 4000
BUILD ?= build
RTL := $(shell cat rtl.f)
TOP := banked_matching_engine
TB := tb_banked_matching_engine
PROFILE ?= default
RESERVE_TOP := reserve_matching_engine
RESERVE_TB := tb_reserve_matching_engine
RESERVE_RTL := $(shell cat rtl_reserve.f)
RESERVE_BUILD := $(BUILD)/reserve
RESERVE_SEED ?= 9253
RESERVE_COMMANDS ?= 2400
RESERVE_PROFILES := default odd tiny
AUCTION_BANKED_RTL := $(shell cat rtl_auction_banked.f)
AUCTION_BANKED_BUILD := $(BUILD)/auction-banked

.PHONY: all test legacy-test unit regress lint synth route report clean
.PHONY: reserve-test reserve-regress reserve-unit reserve-demo lint-reserve synth-reserve formal-reserve waveform-reserve
.PHONY: manage-demo waveform-management
.PHONY: auction-test auction-example auction-bridge auction-lint auction-synth formal-auction waveform-auction
.PHONY: banked-auction-test banked-auction-lint banked-auction-synth formal-banked-auction waveform-banked-auction
all: test

test: legacy-test reserve-test auction-test banked-auction-test

legacy-test: unit $(BUILD)/$(PROFILE)/engine.vvp
	$(PYTHON) -B verify_matching.py run --profile $(PROFILE) --seed $(SEED) --commands $(COMMANDS) \
		--vvp "$(VVP)" --sim $(BUILD)/$(PROFILE)/engine.vvp --build $(BUILD)/$(PROFILE)

regress: reserve-regress auction-test banked-auction-test
	$(MAKE) legacy-test PROFILE=default SEED=7201
	$(MAKE) legacy-test PROFILE=default SEED=39119
	$(MAKE) legacy-test PROFILE=wide SEED=9109
	$(MAKE) legacy-test PROFILE=tiny SEED=9887

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

lint: lint-reserve auction-lint banked-auction-lint
	$(VERILATOR) --lint-only -Wall --top-module $(TOP) -f rtl.f

synth: synth-reserve auction-synth banked-auction-synth
	mkdir -p $(BUILD)
	$(YOSYS) -Q -l $(BUILD)/synth.log -p 'read_verilog -sv $(RTL); synth -top $(TOP); check -assert; stat; write_json $(BUILD)/$(TOP).json'

$(RESERVE_BUILD)/%/engine.vvp: $(RESERVE_RTL) reserve_codes.svh $(RESERVE_TB).sv rtl_reserve.f reserve_verify.py reserve_model.py Makefile
	mkdir -p $(dir $@)
	params=$$($(PYTHON) -B reserve_verify.py params --profile $*) && \
	$(IVERILOG) -g2012 -s $(RESERVE_TB) $$params -o $@ -f rtl_reserve.f $(RESERVE_TB).sv

reserve-unit:
	$(PYTHON) -B -m unittest -v test_reserve_model test_reserve_checkpoint test_reserve_management

reserve-test: reserve-unit reserve-demo manage-demo $(RESERVE_BUILD)/default/engine.vvp
	$(PYTHON) -B reserve_verify.py run --profile default --seed $(RESERVE_SEED) --commands $(RESERVE_COMMANDS) \
		--vvp "$(VVP)" --sim $(RESERVE_BUILD)/default/engine.vvp --build $(RESERVE_BUILD)/default

reserve-regress: reserve-unit reserve-demo manage-demo $(foreach p,$(RESERVE_PROFILES),$(RESERVE_BUILD)/$(p)/engine.vvp)
	@set -e; for profile in $(RESERVE_PROFILES); do \
		$(PYTHON) -B reserve_verify.py run --profile $$profile --seed $(RESERVE_SEED) --commands $(RESERVE_COMMANDS) \
			--vvp "$(VVP)" --sim $(RESERVE_BUILD)/$$profile/engine.vvp --build $(RESERVE_BUILD)/$$profile; \
	done

reserve-demo: $(RESERVE_BUILD)/default/engine.vvp
	$(PYTHON) -B reserve_demo.py --vvp "$(VVP)" --sim $< --build $(RESERVE_BUILD)/example

manage-demo: $(RESERVE_BUILD)/default/engine.vvp
	$(PYTHON) -B reserve_management_demo.py --vvp "$(VVP)" --sim $< --build $(RESERVE_BUILD)/managed

waveform-management: $(RESERVE_BUILD)/default/engine.vvp ../scripts/render_vcd_waveform.py
	$(PYTHON) -B reserve_management_demo.py --vvp "$(VVP)" --sim $< --build $(RESERVE_BUILD)/managed-wave \
		--waveform $(RESERVE_BUILD)/managed-wave/management.vcd
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(RESERVE_BUILD)/managed-wave/management.vcd docs/management_waveform.svg \
		--start 170 --end 510 --title 'Day 02 · Priority-preserving replace and scoped withdrawal' \
		--signal 'tb_reserve_matching_engine.clk=Clock' \
		--signal 'tb_reserve_matching_engine.m_ready=Event ready' \
		--signal 'tb_reserve_matching_engine.m_valid=Event valid' \
		--signal 'tb_reserve_matching_engine.m_tag=Command tag' \
		--signal 'tb_reserve_matching_engine.m_kind=Event kind' \
		--signal 'tb_reserve_matching_engine.m_maker=Participating order ID' \
		--signal 'tb_reserve_matching_engine.m_refresh=Priority renewed' \
		--signal 'tb_reserve_matching_engine.m_event_count=Rank or row count' \
		--signal 'tb_reserve_matching_engine.m_last=Terminal event'

waveform-reserve: $(RESERVE_BUILD)/default/engine.vvp ../scripts/render_vcd_waveform.py
	$(PYTHON) -B reserve_demo.py --vvp "$(VVP)" --sim $< --build $(RESERVE_BUILD)/waveform \
		--waveform $(RESERVE_BUILD)/waveform/source.vcd
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(RESERVE_BUILD)/waveform/source.vcd docs/reserve_waveform.svg \
		--start 165 --end 275 --title 'Day 02 · Reserve priority and checkpoint order' \
		--signal 'tb_reserve_matching_engine.clk=Clock' \
		--signal 'tb_reserve_matching_engine.m_valid=Event valid' \
		--signal 'tb_reserve_matching_engine.m_ready=Event ready' \
		--signal 'tb_reserve_matching_engine.m_tag=Command tag' \
		--signal 'tb_reserve_matching_engine.m_kind=Event kind' \
		--signal 'tb_reserve_matching_engine.m_maker=Maker or row ID' \
		--signal 'tb_reserve_matching_engine.m_quantity=Fill quantity' \
		--signal 'tb_reserve_matching_engine.m_refresh=Reserve refilled' \
		--signal 'tb_reserve_matching_engine.m_last=Terminal event'

lint-reserve:
	$(VERILATOR) --lint-only -Wall --top-module $(RESERVE_TOP) -f rtl_reserve.f

synth-reserve:
	mkdir -p $(RESERVE_BUILD)
	$(YOSYS) -Q -q -l $(RESERVE_BUILD)/synth.log -p 'read_verilog -sv $(RESERVE_RTL); synth -top $(RESERVE_TOP) -noabc; check -assert; stat; write_json $(RESERVE_BUILD)/$(RESERVE_TOP).json'
	tail -n 24 $(RESERVE_BUILD)/synth.log

formal-reserve:
	mkdir -p $(RESERVE_BUILD)
	cd formal && $(SBY) -f --prefix ../$(RESERVE_BUILD)/formal reserve.sby

auction-test: auction-bridge
	$(PYTHON) -B -m unittest -v test_auction_model
	$(PYTHON) -B auction_verify.py --iverilog "$(IVERILOG)" --vvp "$(VVP)" --build $(BUILD)/auction

auction-example:
	$(PYTHON) -B auction_verify.py --example --iverilog "$(IVERILOG)" --vvp "$(VVP)" --build $(BUILD)/auction

auction-bridge: auction-example $(RESERVE_BUILD)/default/engine.vvp
	$(PYTHON) -B auction_bridge_demo.py --capture $(BUILD)/auction/example/capture.txt \
		--build $(BUILD)/auction/handoff --sim $(RESERVE_BUILD)/default/engine.vvp --vvp "$(VVP)"

auction-lint:
	$(VERILATOR) --lint-only -Wall --top-module call_auction_engine -f rtl_auction.f

auction-synth:
	mkdir -p $(BUILD)/auction
	$(YOSYS) -Q -q -l $(BUILD)/auction/synth.log -p 'read_verilog -sv call_auction_engine.sv; synth -top call_auction_engine -noabc; check -assert; stat; write_json $(BUILD)/auction/netlist.json'
	tail -n 24 $(BUILD)/auction/synth.log

formal-auction:
	mkdir -p $(BUILD)/auction
	cd formal && $(SBY) -f --prefix ../$(BUILD)/auction/formal auction.sby

waveform-auction: ../scripts/render_vcd_waveform.py
	$(PYTHON) -B auction_verify.py --example --iverilog "$(IVERILOG)" --vvp "$(VVP)" \
		--build $(BUILD)/auction-wave --wave $(BUILD)/auction-wave/auction.vcd
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/auction-wave/auction.vcd docs/auction_waveform.svg \
		--start 675 --end 850 --title 'Day 02 · Uniform-price uncross and residuals' \
		--signal 'tb_call_auction_engine.clk=Clock' \
		--signal 'tb_call_auction_engine.m_valid=Event valid' \
		--signal 'tb_call_auction_engine.m_ready=Event ready' \
		--signal 'tb_call_auction_engine.m_kind=Event kind' \
		--signal 'tb_call_auction_engine.m_price=Clearing or limit tick' \
		--signal 'tb_call_auction_engine.m_first=Buy or residual ID' \
		--signal 'tb_call_auction_engine.m_second=Sell ID' \
		--signal 'tb_call_auction_engine.m_quantity=Event quantity' \
		--signal 'tb_call_auction_engine.m_last=Terminal event'

banked-auction-test:
	$(PYTHON) -B -m unittest -v test_banked_auction
	$(PYTHON) -B banked_auction_verify.py --iverilog "$(IVERILOG)" --vvp "$(VVP)" --build $(AUCTION_BANKED_BUILD)
	$(PYTHON) -B banked_auction_verify.py --pressure --iverilog "$(IVERILOG)" --vvp "$(VVP)" --build $(AUCTION_BANKED_BUILD)
	$(PYTHON) -B banked_auction_verify.py --example --iverilog "$(IVERILOG)" --vvp "$(VVP)" --build $(AUCTION_BANKED_BUILD)

banked-auction-lint:
	$(VERILATOR) --lint-only -Wall --top-module banked_call_auction -f rtl_auction_banked.f

banked-auction-synth:
	mkdir -p $(AUCTION_BANKED_BUILD)
	$(YOSYS) -Q -q -l $(AUCTION_BANKED_BUILD)/synth.log -p 'read_verilog -sv $(AUCTION_BANKED_RTL); synth -top banked_call_auction -noabc; check -assert; stat; write_json $(AUCTION_BANKED_BUILD)/netlist.json'
	tail -n 24 $(AUCTION_BANKED_BUILD)/synth.log

formal-banked-auction:
	mkdir -p $(AUCTION_BANKED_BUILD)
	cd formal && $(SBY) -f --prefix ../$(AUCTION_BANKED_BUILD)/formal banked_auction.sby

waveform-banked-auction: ../scripts/render_vcd_waveform.py
	$(PYTHON) -B banked_auction_verify.py --example --iverilog "$(IVERILOG)" --vvp "$(VVP)" \
		--build $(AUCTION_BANKED_BUILD)/waveform --wave $(AUCTION_BANKED_BUILD)/waveform/cohort.vcd
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(AUCTION_BANKED_BUILD)/waveform/cohort.vcd docs/cohort_waveform.svg \
		--start 165 --end 650 --title 'Day 02 · Scheduled two-instrument auction close' \
		--signal 'tb_banked_call_auction.ctl_report_status=Control status' \
		--signal 'tb_banked_call_auction.schedule_armed=Armed bank mask' \
		--signal 'tb_banked_call_auction.schedule_due=Due bank mask' \
		--signal 'tb_banked_call_auction.schedule_queued=Queued close mask' \
		--signal 'tb_banked_call_auction.m_ready=Sink ready' \
		--signal 'tb_banked_call_auction.m_valid=Event valid' \
		--signal 'tb_banked_call_auction.m_bank=Event bank' \
		--signal 'tb_banked_call_auction.m_scheduled=Automatic close event' \
		--signal 'tb_banked_call_auction.event_kind=Event kind' \
		--signal 'tb_banked_call_auction.m_wrapper_status=Wrapper status' \
		--signal 'tb_banked_call_auction.event_last=Terminal event'

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

.PHONY: waveform

waveform: ../scripts/render_vcd_waveform.py
	$(MAKE) PROFILE=default $(BUILD)/default/engine.vvp
	$(PYTHON) -B verify_matching.py run --profile default --seed 7201 --commands 0 --vvp "$(VVP)" --sim $(BUILD)/default/engine.vvp --build $(BUILD)/waveform --waveform $(BUILD)/banked_matching_engine.vcd
	mkdir -p docs
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/banked_matching_engine.vcd docs/waveform.svg \
		--start 655 --end 800 --title 'Day 02 · A fill sequence shares the egress lane' \
		--signal 'tb_banked_matching_engine.clk=Clock' \
		--signal 'tb_banked_matching_engine.commits=Bank commit mask' \
		--signal 'tb_banked_matching_engine.m_valid=Event valid' \
		--signal 'tb_banked_matching_engine.m_ready=Event ready' \
		--signal 'tb_banked_matching_engine.m_instrument=Event instrument' \
		--signal 'tb_banked_matching_engine.m_tag=Command tag' \
		--signal 'tb_banked_matching_engine.m_kind=Event kind' \
		--signal 'tb_banked_matching_engine.m_last=Terminal event'
