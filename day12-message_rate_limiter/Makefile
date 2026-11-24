PYTHON ?= python3
IVERILOG ?= iverilog
VVP ?= vvp
VERILATOR ?= verilator
YOSYS ?= yosys
SBY ?= sby
SEED ?= 121203
OPERATIONS ?= 1500
RTL := $(shell cat rtl.f)
TOP := message_rate_limiter

.PHONY: all test test-python test-refill test-matrix test-workload test-gate test-netlist lint synth formal clean
all: test

test: test-python test-refill test-matrix test-workload test-gate

test-python:
	$(PYTHON) -B -m unittest discover

test-refill: build/refill.vvp
	$(VVP) $<

test-matrix:
	$(PYTHON) -B run_limiter_matrix.py --iverilog "$(IVERILOG)" --vvp "$(VVP)" \
		--seed $(SEED) --operations $(OPERATIONS)

test-workload:
	$(PYTHON) -B limiter_cli.py sample build/demo
	$(PYTHON) -B limiter_cli.py prepare build/demo/workload.csv build/demo-run --stalls build/demo/stalls.csv --run --iverilog "$(IVERILOG)" --vvp "$(VVP)" > build/demo-run.log

test-gate:
	$(PYTHON) -B run_gate_matrix.py --iverilog "$(IVERILOG)" --vvp "$(VVP)"

test-netlist: synth
	$(PYTHON) -B run_limiter_netlist.py --iverilog "$(IVERILOG)" --vvp "$(VVP)" --seed $(SEED)

build:
	mkdir -p $@

build/refill.vvp: rate_refill.sv tb_rate_refill.sv | build
	$(IVERILOG) -g2012 -Wall -s tb_rate_refill -o $@ $^

lint:
	$(VERILATOR) --lint-only -Wall --top-module $(TOP) -f rtl.f
	$(VERILATOR) --lint-only -Wall --top-module message_rate_gate -f rtl.f

synth: | build
	$(YOSYS) -Q -q -T -l build/rate-limiter-synth.log -m slang -p 'read_slang --top $(TOP) $(RTL); synth -top $(TOP); check -assert; stat; write_json build/limiter.json; write_verilog -noattr build/limiter-netlist.v'
	$(YOSYS) -Q -q -T -l build/rate-gate-synth.log -m slang -p 'read_slang --top message_rate_gate $(RTL); synth -top message_rate_gate; check -assert; stat; write_json build/gate.json'

formal: | build
	cd formal && $(SBY) -f --prefix ../build/formal limiter.sby

clean:
	rm -rf build

.PHONY: waveform
waveform:
	$(PYTHON) -B run_gate_matrix.py --iverilog "$(IVERILOG)" --vvp "$(VVP)" --wave build/forwarding-gate.vcd
	$(PYTHON) -B ../scripts/render_vcd_waveform.py build/forwarding-gate.vcd waveform.svg \
		--start 11 --end 60 --title 'Rate gate · Independent audit and payload handshakes' \
		--signal 'tb_message_rate_gate.clk=Clock' \
		--signal 'tb_message_rate_gate.s_valid=Request valid' \
		--signal 'tb_message_rate_gate.s_ready=Request ready' \
		--signal 'tb_message_rate_gate.s_tag=Request tag' \
		--signal 'tb_message_rate_gate.audit_valid=Audit valid' \
		--signal 'tb_message_rate_gate.audit_ready=Audit ready' \
		--signal 'tb_message_rate_gate.audit_tag=Audit tag' \
		--signal 'tb_message_rate_gate.audit_status=Audit status' \
		--signal 'tb_message_rate_gate.out_valid=Forward valid' \
		--signal 'tb_message_rate_gate.out_ready=Forward ready' \
		--signal 'tb_message_rate_gate.out_tag=Forward tag'
