PYTHON ?= python3
IVERILOG ?= iverilog
VVP ?= vvp
VERILATOR ?= verilator
YOSYS ?= yosys
SEED ?= 0xd03a11
OPERATIONS ?= 3600
RTL := $(shell cat rtl.f)
TOP := hft_order_risk_gateway

.PHONY: all test lint synth clean test-host test-narrow test-wide test-boundaries test-tap test-gate
all: test

test: test-host test-narrow test-wide test-boundaries test-tap

test-host:
	$(PYTHON) -m unittest test_audit_trace

test-narrow: build/narrow.vvp
	$(PYTHON) gen_risk_vectors.py --profile narrow --output build/narrow --seed $(SEED) --operations $(OPERATIONS)
	$(VVP) $< +vector_dir=build/narrow
	$(PYTHON) audit_trace.py check --profile narrow --capture build/narrow/audit.hex --expected build/narrow/expected.txt

test-wide: build/wide.vvp
	$(PYTHON) gen_risk_vectors.py --profile wide --output build/wide --seed $(SEED) --operations $(OPERATIONS)
	$(VVP) $< +vector_dir=build/wide
	$(PYTHON) audit_trace.py check --profile wide --capture build/wide/audit.hex --expected build/wide/expected.txt

test-boundaries: build/boundaries.vvp
	$(VVP) $<

test-tap: build/tap.vvp build/tap-one.vvp
	$(VVP) build/tap.vvp
	$(VVP) build/tap-one.vvp

test-gate: build/gate.vvp
	$(PYTHON) gen_risk_vectors.py --profile narrow --output build/gate --operations 100
	$(VVP) $< +vector_dir=build/gate
	$(PYTHON) audit_trace.py check --profile narrow --capture build/gate/audit.hex --expected build/gate/expected.txt

build:
	mkdir -p $@

build/narrow.vvp: rtl.f $(RTL) tb_order_risk_gateway.sv | build
	$(IVERILOG) -g2012 -Wall -s tb_order_risk_gateway -o $@ -f rtl.f tb_order_risk_gateway.sv

build/wide.vvp: rtl.f $(RTL) tb_order_risk_gateway.sv | build
	$(IVERILOG) -g2012 -Wall -s tb_order_risk_gateway -Ptb_order_risk_gateway.PRICE_W=24 \
		-Ptb_order_risk_gateway.QTY_W=16 -o $@ -f rtl.f tb_order_risk_gateway.sv

build/boundaries.vvp: rtl.f $(RTL) tb_risk_boundaries.sv | build
	$(IVERILOG) -g2012 -Wall -s tb_risk_boundaries -o $@ -f rtl.f tb_risk_boundaries.sv

build/tap.vvp: rtl.f $(RTL) tb_risk_audit_tap.sv | build
	$(IVERILOG) -g2012 -Wall -s tb_risk_audit_tap -o $@ -f rtl.f tb_risk_audit_tap.sv

build/tap-one.vvp: rtl.f $(RTL) tb_risk_audit_tap.sv | build
	$(IVERILOG) -g2012 -Wall -s tb_risk_audit_tap -Ptb_risk_audit_tap.DEPTH=1 \
		-o $@ -f rtl.f tb_risk_audit_tap.sv

build/narrow-gates.v: hft_order_risk_gateway.sv | build
	$(YOSYS) -Q -q -T -l build/narrow-gates.log -p 'read_verilog -sv $<; chparam -set NUM_ACCOUNTS 3 -set NUM_INSTRUMENTS 3 -set ORDER_SLOTS 5 -set PRICE_W 8 -set QTY_W 8 -set GEN_W 3 -set EVENT_W 4 $(TOP); synth -top $(TOP); check -assert; write_verilog -noattr $@'

build/gate.vvp: build/narrow-gates.v risk_audit_tap.sv risk_audit_serializer.sv tb_order_risk_gateway.sv
	$(IVERILOG) -g2012 -DRISK_POST_SYNTH -s tb_order_risk_gateway -o $@ $^

lint:
	$(VERILATOR) --lint-only -Wall --top-module $(TOP) -f rtl.f
	$(VERILATOR) --lint-only -Wall --top-module risk_audit_tap -f rtl.f
	$(VERILATOR) --lint-only -Wall --top-module risk_audit_serializer -f rtl.f

synth: | build
	$(YOSYS) -Q -q -T -l build/gateway-synth.log -p 'read_verilog -sv $(RTL); synth -top $(TOP); check -assert; stat; write_json build/gateway.json'
	$(YOSYS) -Q -q -T -l build/audit-tap-synth.log -p 'read_verilog -sv $(RTL); synth -top risk_audit_tap; check -assert; stat'
	$(YOSYS) -Q -q -T -l build/audit-serializer-synth.log -p 'read_verilog -sv $(RTL); synth -top risk_audit_serializer; check -assert; stat'

clean:
	rm -rf build

.PHONY: waveform
waveform: build/boundaries.vvp
	$(VVP) $< +wave=build/risk-halt.vcd
	$(PYTHON) -B ../scripts/render_vcd_waveform.py build/risk-halt.vcd waveform.svg \
		--start 390 --end 525 --title 'Risk gateway · Halt between admission and commit' \
		--signal 'tb_risk_boundaries.clk=Clock' \
		--signal 'tb_risk_boundaries.s_valid=Request valid' \
		--signal 'tb_risk_boundaries.s_ready=Request ready' \
		--signal 'tb_risk_boundaries.s_tag=Request tag' \
		--signal 'tb_risk_boundaries.halt_req=Halt request' \
		--signal 'tb_risk_boundaries.halted=Halted' \
		--signal 'tb_risk_boundaries.m_valid=Reply valid' \
		--signal 'tb_risk_boundaries.m_ready=Reply ready' \
		--signal 'tb_risk_boundaries.m_tag=Reply tag' \
		--signal 'tb_risk_boundaries.m_code=Reply code' \
		--signal 'tb_risk_boundaries.m_open_orders=Open orders'
