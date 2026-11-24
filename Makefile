PYTHON ?= python3
IVERILOG ?= iverilog
VVP ?= vvp
YOSYS ?= yosys
NEXTPNR ?= nextpnr-ecp5
SBY ?= sby
FREQ ?= 100
SEEDS ?= 1
ROUTE_JOBS ?= 1

FEED := day01-redundant_market_data
BOOK := day02-banked_matching_engine
RISK := day03-order_risk_gateway
ETHERNET := day04-ethernet_udp_frontend
MBO := day05-market_state_quote_engine
LATENCY := day06-multiclock_latency_telemetry
OUCH := day07-ouch_order_session
DMA := day08-host_dma_queues
LEGACY := $(FEED) $(BOOK) $(RISK)
EXPANSION := $(ETHERNET) $(MBO) $(LATENCY) $(OUCH) $(DMA)
BEGINNER := day10-stream_width_converter day11-best_quote_selector day12-message_rate_limiter \
            day13-timestamped_stream_replayer day14-fixed_point_notional
PIPELINE := day15-multicast_packet_fanout day16-credit_order_scheduler day17-drop_copy_reconciliation
PROJECTS ?= $(LEGACY) $(EXPANSION) $(BEGINNER) $(PIPELINE)
HDL_HEADERS := $(wildcard $(BOOK)/*.svh $(RISK)/*.svh)
BOOK_RTL = $(addprefix $(BOOK)/,$(shell sed -e 's/\#.*//' $(BOOK)/rtl.f))
RISK_RTL = $(addprefix $(RISK)/,$(shell sed -e 's/\#.*//' $(RISK)/rtl.f))

.PHONY: all verify test lint synth formal inventory route demo netlist-check waveform waveform-tools clean
all: test
verify: inventory test lint synth formal netlist-check

define project_targets
.PHONY: test-$(1) lint-$(1) synth-$(1) waveform-$(1) clean-$(1)
test: test-$(1)
test-$(1):
	$$(MAKE) -C $(1) $(if $(filter $(1),$(FEED) $(BOOK) $(MBO)),regress,test)
lint: lint-$(1)
lint-$(1):
	$$(MAKE) -C $(1) lint
synth: synth-$(1)
synth-$(1):
	$$(MAKE) -C $(1) synth YOSYS="$$(YOSYS)"
waveform: waveform-$(1)
waveform-$(1):
	$$(MAKE) -C $(1) waveform
clean: clean-$(1)
clean-$(1):
	$$(MAKE) -C $(1) clean
endef

$(foreach project,$(PROJECTS),$(eval $(call project_targets,$(project))))

test: demo

inventory:
	$(PYTHON) scripts/source_inventory.py --enforce

waveform: waveform-tools
waveform-tools:
	$(PYTHON) -B -m unittest discover -s scripts -p test_render_vcd_waveform.py

route:
	$(PYTHON) scripts/characterize_fpga.py --yosys "$(YOSYS)" --nextpnr "$(NEXTPNR)" --frequency $(FREQ) --seeds $(SEEDS) --jobs $(ROUTE_JOBS)

formal:
	mkdir -p $(BOOK)/build $(RISK)/build
	cd $(BOOK)/formal && $(SBY) -f --prefix ../build/fifo fifo.sby prove cover
	cd $(BOOK)/formal && $(SBY) -f --prefix ../build/arbiter arbiter.sby prove cover
	cd $(RISK)/formal && $(SBY) -f --prefix ../build/gateway gateway.sby bmc cover
	$(MAKE) -C $(MBO) formal SBY="$(SBY)"
	@set -e; for project in $(BEGINNER) $(PIPELINE); do $(MAKE) -C "$$project" formal SBY="$(SBY)"; done

netlist-check:
	$(MAKE) -C $(RISK) test-gate YOSYS="$(YOSYS)"

demo: build/risk_match_lifecycle.vvp
	$(VVP) $<

build/risk_match_lifecycle.vvp: demo/tb_risk_match_lifecycle.sv $(BOOK_RTL) $(RISK_RTL) $(HDL_HEADERS)
	mkdir -p build
	$(IVERILOG) -g2012 -I $(BOOK) -I $(RISK) -s tb_risk_match_lifecycle -o $@ $(BOOK_RTL) $(RISK_RTL) $<

clean:
	rm -rf build
