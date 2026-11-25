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

test: basket-test fee-tier-test $(BUILD)/pair.vvp $(foreach frac,$(PROFILES),$(BUILD)/q$(frac).vvp) $(BUILD)/divide-small.vvp $(BUILD)/divide-wide.vvp
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
waveform: demo basket-waveform fee-tier-waveform
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/notional-demo.vcd waveform.svg \
		--start 2130 --end 2250 --title "Rounded notional response waits for the consumer" \
		--signal '$(WAVE_SCOPE).clk=Clock' --signal '$(WAVE_SCOPE).req_ready=Request ready' \
		--signal '$(WAVE_SCOPE).req_valid=Request valid' --signal '$(WAVE_SCOPE).rsp_valid=Response valid' \
		--signal '$(WAVE_SCOPE).rsp_ready=Response ready' --signal '$(WAVE_SCOPE).rsp_tag=Response tag' \
		--signal '$(WAVE_SCOPE).rsp_gross=Gross' --signal '$(WAVE_SCOPE).rsp_fee=Fee' \
		--signal '$(WAVE_SCOPE).rsp_cash=Cash debit'

lint: basket-lint fee-tier-lint
	$(VERILATOR) --lint-only -Wall -I. --top-module $(TOP) -f rtl.f
	$(VERILATOR) --lint-only -Wall -I. --top-module notional_pair -f rtl.f

synth: basket-synth fee-tier-synth | $(BUILD)
	$(YOSYS) -Q -q -l $(BUILD)/synth.log -m slang -p 'read_slang -I. --top $(TOP) $(RTL); synth -top $(TOP) -noabc; check -assert; stat'
	$(YOSYS) -Q -q -l $(BUILD)/pair-synth.log -m slang -p 'read_slang -I. --top notional_pair $(RTL); synth -top notional_pair -noabc; check -assert; stat'
	tail -n 20 $(BUILD)/synth.log
	tail -n 20 $(BUILD)/pair-synth.log

formal: | $(BUILD)
	cd formal && $(SBY) -f --prefix ../$(BUILD)/division divide.sby prove cover

clean:
	rm -rf $(BUILD)

BASKET_RTL := notional_basket.sv fixed_point_notional.sv notional_divide.sv
BASKET_TB := tb_notional_basket
BASKET_SCOPE := $(BASKET_TB)
.PHONY: basket-test basket-lint basket-synth basket-waveform

define basket
$(BUILD)/basket-$(1).vvp: $(BASKET_TB).sv $(BASKET_RTL) $(HEADERS) | $(BUILD)
	$(IVERILOG) -g2012 -I. -s $(BASKET_TB) -P$(BASKET_TB).MAX_LEGS=$(2) \
		-P$(BASKET_TB).PRICE_FRAC=$(3) -o $$@ $$(filter %.sv,$$^)

$(BUILD)/basket-$(1).txt: notional_basket_vectors.py notional_model.py | $(BUILD)
	$(PYTHON) -B notional_basket_vectors.py $$@ --maximum $(2) --fraction $(3) --seed $(4)

.PHONY: basket-$(1)
basket-$(1): $(BUILD)/basket-$(1).vvp $(BUILD)/basket-$(1).txt
	$(VVP) $$< +tape=$(BUILD)/basket-$(1).txt
endef

$(eval $(call basket,main,8,8,440601))
$(eval $(call basket,odd,3,0,77237))
$(eval $(call basket,tiny,1,31,48112))

basket-test: basket-main basket-odd basket-tiny

basket-lint:
	$(VERILATOR) --lint-only -Wall -I. --top-module notional_basket $(BASKET_RTL)

basket-synth: | $(BUILD)
	$(YOSYS) -Q -q -l $(BUILD)/basket-synth.log -m slang -p 'read_slang -I. --top notional_basket $(BASKET_RTL); synth -top notional_basket -noabc; check -assert; stat'

$(BUILD)/basket-lesson.txt: notional_basket_vectors.py notional_model.py | $(BUILD)
	$(PYTHON) -B notional_basket_vectors.py $@ --maximum 8 --fraction 8 --count 0

basket-waveform: $(BUILD)/basket-main.vvp $(BUILD)/basket-lesson.txt
	$(VVP) $< +tape=$(BUILD)/basket-lesson.txt +wave=$(BUILD)/basket.vcd
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/basket.vcd basket-waveform.svg \
		--start 20 --end 660 --title 'Notional basket · Opposing legs offset before final cash check' \
		--signal '$(BASKET_SCOPE).clk=Clock' --signal '$(BASKET_SCOPE).req_valid=Basket header' \
		--signal '$(BASKET_SCOPE).leg_valid=Leg valid' --signal '$(BASKET_SCOPE).leg_ready=Leg ready' \
		--signal '$(BASKET_SCOPE).leg_sell=Sell leg' --signal '$(BASKET_SCOPE).rsp_gross=Rounded gross sum' \
		--signal '$(BASKET_SCOPE).rsp_net_cash=Signed net debit' \
		--signal '$(BASKET_SCOPE).rsp_valid=Result valid' --signal '$(BASKET_SCOPE).rsp_ready=Result ready'

FEE_RTL := tiered_fee_settlement.sv notional_divide.sv
FEE_TB := tb_tiered_fee_settlement
.PHONY: fee-tier-test fee-tier-lint fee-tier-synth fee-tier-waveform

define fee_case
$(BUILD)/fee-$(1).vvp: $(FEE_TB).sv $(FEE_RTL) | $(BUILD)
	$(IVERILOG) -g2012 -s $(FEE_TB) -P$(FEE_TB).VENUES=$(2) -P$(FEE_TB).TIERS=$(3) \
		-P$(FEE_TB).PRICE_FRAC=$(4) -o $$@ $$^

$(BUILD)/fee-$(1).hex: fee_tier_vectors.py notional_model.py | $(BUILD)
	$(PYTHON) -B fee_tier_vectors.py $$@ --venues $(2) --tiers $(3) --fraction $(4) --seed $(5)

.PHONY: fee-tier-$(1)
fee-tier-$(1): $(BUILD)/fee-$(1).vvp $(BUILD)/fee-$(1).hex
	$(VVP) $$< +vectors=$(BUILD)/fee-$(1).hex
endef

$(eval $(call fee_case,main,3,4,8,57013))
$(eval $(call fee_case,wide,1,3,0,72042))
$(eval $(call fee_case,tiny,2,1,31,15067))

fee-tier-test: fee-tier-main fee-tier-wide fee-tier-tiny

fee-tier-lint:
	$(VERILATOR) --lint-only -Wall --top-module tiered_fee_settlement $(FEE_RTL)

fee-tier-synth: | $(BUILD)
	$(YOSYS) -Q -q -l $(BUILD)/fee-tier-synth.log -m slang -p 'read_slang --top tiered_fee_settlement $(FEE_RTL); synth -top tiered_fee_settlement -noabc; check -assert; stat'

fee-tier-waveform: $(BUILD)/fee-main.vvp $(BUILD)/fee-main.hex
	$(VVP) $< +vectors=$(BUILD)/fee-main.hex +wave=$(BUILD)/fee.vcd
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/fee.vcd fee-tier-waveform.svg \
		--start 55 --end 1510 --title 'Tiered fees · Split turnover across rate bands before rounding' \
		--signal '$(FEE_TB).s_valid=Fill request' --signal '$(FEE_TB).s_ready=Fill ready' \
		--signal '$(FEE_TB).dut.tier_index=Tier index' --signal '$(FEE_TB).dut.slice=Gross allocated to tier' \
		--signal '$(FEE_TB).m_gross=Gross notional' --signal '$(FEE_TB).m_fee=Net signed fee' \
		--signal '$(FEE_TB).m_cash=Cash amount' --signal '$(FEE_TB).m_tiers=Tiers touched' \
		--signal '$(FEE_TB).m_valid=Settlement valid' --signal '$(FEE_TB).m_ready=Settlement ready'
