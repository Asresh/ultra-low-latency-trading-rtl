PYTHON ?= python3
IVERILOG ?= iverilog
VVP ?= vvp
VERILATOR ?= verilator
YOSYS ?= yosys
NEXTPNR ?= nextpnr-ecp5
SEED ?= 0x0ac405
OPERATIONS ?= 900
ROUTE_FREQ ?= 100
ROUTE_SEED ?= 1
TOP := ouch_order_session
RTL := $(shell cat rtl.f)
VFLAGS := -g2012 -Wall -Wno-sensitivity-entire-array

.PHONY: waveform wave-vcd all test test-host test-session test-framing test-decimal lint synth timing clean
all: test

test: test-host test-session test-framing test-decimal

test-host:
	$(PYTHON) -m unittest test_ouch_protocol test_ouch_audit

test-session: build/session.vvp build/session-four.vvp build/session-twelve.vvp
	$(PYTHON) gen_ouch_vectors.py --output build/session-eight --slots 8 --operations $(OPERATIONS) --seed $(SEED)
	$(VVP) build/session.vvp +vectors=build/session-eight
	$(PYTHON) ouch_audit.py check-tx build/session-eight/hardware_tx.hex build/session-eight/tx.txt
	$(PYTHON) gen_ouch_vectors.py --output build/session-four --slots 4 --operations $(OPERATIONS) --seed 0x19e503
	$(VVP) build/session-four.vvp +vectors=build/session-four +stall_seed=17951
	$(PYTHON) ouch_audit.py check-tx build/session-four/hardware_tx.hex build/session-four/tx.txt
	$(PYTHON) gen_ouch_vectors.py --output build/session-twelve --slots 12 --operations $(OPERATIONS) --seed 0x69e4af
	$(VVP) build/session-twelve.vvp +vectors=build/session-twelve +stall_seed=27391
	$(PYTHON) ouch_audit.py check-tx build/session-twelve/hardware_tx.hex build/session-twelve/tx.txt

test-framing: build/framing.vvp
	$(VVP) $<

test-decimal: build/decimal.vvp
	$(VVP) $<

build:
	mkdir -p $@

build/session.vvp: $(RTL) tb_ouch_order_session.sv rtl.f | build
	$(IVERILOG) $(VFLAGS) -s tb_ouch_order_session -o $@ -f rtl.f tb_ouch_order_session.sv

build/session-four.vvp: $(RTL) tb_ouch_order_session.sv rtl.f | build
	$(IVERILOG) $(VFLAGS) -s tb_ouch_order_session -Ptb_ouch_order_session.ORDER_SLOTS=4 -o $@ -f rtl.f tb_ouch_order_session.sv

build/session-twelve.vvp: $(RTL) tb_ouch_order_session.sv rtl.f | build
	$(IVERILOG) $(VFLAGS) -s tb_ouch_order_session -Ptb_ouch_order_session.ORDER_SLOTS=12 -o $@ -f rtl.f tb_ouch_order_session.sv

build/framing.vvp: $(RTL) tb_ouch_framing.sv rtl.f | build
	$(IVERILOG) $(VFLAGS) -s tb_ouch_framing -o $@ -f rtl.f tb_ouch_framing.sv

build/decimal.vvp: $(RTL) tb_ouch_decimal64.sv rtl.f | build
	$(IVERILOG) $(VFLAGS) -s tb_ouch_decimal64 -o $@ -f rtl.f tb_ouch_decimal64.sv

lint:
	$(VERILATOR) --lint-only -Wall -Wno-UNUSEDSIGNAL --top-module $(TOP) -f rtl.f

synth: | build
	$(YOSYS) -Q -q -T -m slang -l build/synth.log -p 'read_slang --top $(TOP) $(RTL); synth -top $(TOP); check -assert; stat; write_json build/session-generic.json'

timing: | build
	$(YOSYS) -Q -q -T -m slang -l build/synth-ecp5.log -p 'read_slang --top $(TOP) $(RTL); synth_ecp5 -top $(TOP) -json build/session-ecp5.json; check -assert; stat'
	$(NEXTPNR) --85k --package CABGA381 --speed 8 --threads 1 --out-of-context \
		--freq $(ROUTE_FREQ) --seed $(ROUTE_SEED) --timing-allow-fail \
		--json build/session-ecp5.json --report build/session-route.json --log build/nextpnr.log --quiet

wave-vcd: build/session.vvp
	$(PYTHON) -B gen_ouch_vectors.py --output build/wave --slots 8 --operations $(OPERATIONS) --seed $(SEED)
	$(VVP) $< +vectors=build/wave +waves=build/wave/session.vcd
	$(PYTHON) -B ouch_audit.py check-tx build/wave/hardware_tx.hex build/wave/tx.txt

waveform: wave-vcd
	$(PYTHON) -B ../scripts/render_vcd_waveform.py build/wave/session.vcd waveform.svg \
		--start 13280 --end 15000 --title 'OUCH replay catches up before the live cursor advances' \
		--signal 'tb_ouch_order_session.replaying=Replay Active' \
		--signal 'tb_ouch_order_session.next_sequence=Next Live Sequence' \
		--signal 'tb_ouch_order_session.m_notice_valid=Notice Valid' \
		--signal 'tb_ouch_order_session.m_tx_valid=Client TX Valid' \
		--signal 'tb_ouch_order_session.m_tx_last=Client TX Last' \
		--signal 'tb_ouch_order_session.s_rx_valid=Server RX Valid'

clean:
	rm -rf build
