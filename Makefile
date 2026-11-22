IVERILOG ?= iverilog
VVP ?= vvp
YOSYS ?= yosys
NEXTPNR ?= nextpnr-ecp5
FREQ ?= 100
SEED ?= 1

FEED := projects/01-market-data
BOOK := projects/02-matching-engine
RISK := projects/03-risk-gateway
BOOK_RTL := $(BOOK)/rtl/price_priority.sv $(BOOK)/rtl/price_time_matcher.sv
RISK_RTL := $(RISK)/rtl/pretrade_risk_gateway.sv
FEED_RTL := $(FEED)/rtl/itch_decode.sv
ROUTE_DIR := build/ecp5

.PHONY: all test test-feed test-book test-risk demo synth synth-feed synth-book synth-risk route clean
all: test

test: test-feed test-book test-risk demo

test-feed:
	$(MAKE) -C $(FEED) test SEED=7201
	$(MAKE) -C $(FEED) test SEED=39119

test-book:
	$(MAKE) -C $(BOOK) test

test-risk:
	$(MAKE) -C $(RISK) test

demo: build/risk_to_matcher.vvp
	$(VVP) $<

build/risk_to_matcher.vvp: $(BOOK_RTL) $(RISK_RTL) demo/risk_to_matcher_tb.sv
	mkdir -p build
	$(IVERILOG) -g2012 -s risk_to_matcher_tb -o $@ $^

synth: synth-feed synth-book synth-risk

synth-feed:
	$(MAKE) -C $(FEED) synth YOSYS="$(YOSYS)"

synth-book:
	$(MAKE) -C $(BOOK) synth YOSYS="$(YOSYS)"

synth-risk:
	$(MAKE) -C $(RISK) synth YOSYS="$(YOSYS)"

route: $(ROUTE_DIR)/itch_decode.timing.json $(ROUTE_DIR)/price_time_matcher.timing.json $(ROUTE_DIR)/pretrade_risk_gateway.timing.json
	python3 scripts/summarize_timing.py $^

$(ROUTE_DIR):
	mkdir -p $@

$(ROUTE_DIR)/itch_decode.mapped.json: $(FEED_RTL) Makefile | $(ROUTE_DIR)
	$(YOSYS) -ql $(ROUTE_DIR)/itch_decode.yosys.log -p 'read_verilog -sv $(FEED_RTL); synth_ecp5 -top itch_decode -json $@; check -assert'

$(ROUTE_DIR)/price_time_matcher.mapped.json: $(BOOK_RTL) Makefile | $(ROUTE_DIR)
	$(YOSYS) -ql $(ROUTE_DIR)/price_time_matcher.yosys.log -p 'read_verilog -sv $(BOOK_RTL); synth_ecp5 -top price_time_matcher -json $@; check -assert'

$(ROUTE_DIR)/pretrade_risk_gateway.mapped.json: $(RISK_RTL) Makefile | $(ROUTE_DIR)
	$(YOSYS) -ql $(ROUTE_DIR)/pretrade_risk_gateway.yosys.log -p 'read_verilog -sv $(RISK_RTL); synth_ecp5 -top pretrade_risk_gateway -json $@; check -assert'

# Always reroute so that FREQ and SEED on the command line take effect.
.PHONY: FORCE
$(ROUTE_DIR)/%.timing.json: $(ROUTE_DIR)/%.mapped.json FORCE
	$(NEXTPNR) --85k --package CABGA381 --speed 8 --out-of-context --threads 1 --freq $(FREQ) --seed $(SEED) --timing-allow-fail --json $< --report $@ --log $(ROUTE_DIR)/$*.nextpnr.log -q

clean:
	$(MAKE) -C $(FEED) clean
	$(MAKE) -C $(BOOK) clean
	$(MAKE) -C $(RISK) clean
	rm -rf build
