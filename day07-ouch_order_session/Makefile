PYTHON ?= python3
IVERILOG ?= iverilog
VVP ?= vvp
VERILATOR ?= verilator
YOSYS ?= yosys
NEXTPNR ?= nextpnr-ecp5
SEED ?= 0x0ac405
OPERATIONS ?= 900
REPLAY_OPERATIONS ?= 550
REPLAY_DEPTHS := 1 5 8
ROUTE_FREQ ?= 100
ROUTE_SEED ?= 1
TOP := ouch_order_session
RTL := $(shell cat rtl.f)
VFLAGS := -g2012 -Wall -Wno-sensitivity-entire-array

.PHONY: waveform wave-vcd replay-waveform all test test-host test-session test-replay test-framing test-decimal formal-replay lint synth timing clean
REPLAY_TARGETS := $(addprefix test-replay-,$(REPLAY_DEPTHS))
.PHONY: $(REPLAY_TARGETS)
all: test

test: test-host test-session test-replay test-framing test-decimal

test-host:
	$(PYTHON) -m unittest test_ouch_protocol test_ouch_audit test_ouch_replay

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

test-replay: $(REPLAY_TARGETS)

$(REPLAY_TARGETS): test-replay-%: build/replay-%.vvp
	$(PYTHON) -B gen_replay_vectors.py --output build/replay-$* --depth $* --seed 0x9c004$* --operations $(REPLAY_OPERATIONS)
	$(VVP) $< +vectors=build/replay-$* +stall_seed=1723$*
	$(PYTHON) -B ouch_audit.py check-tx build/replay-$*/hardware_tx.hex build/replay-$*/tx.txt

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

build/replay-%.vvp: $(RTL) tb_ouch_order_session.sv rtl.f | build
	$(IVERILOG) $(VFLAGS) -s tb_ouch_order_session -Ptb_ouch_order_session.REPLAY_HISTORY_DEPTH=$* -o $@ -f rtl.f tb_ouch_order_session.sv

build/framing.vvp: $(RTL) tb_ouch_framing.sv rtl.f | build
	$(IVERILOG) $(VFLAGS) -s tb_ouch_framing -o $@ -f rtl.f tb_ouch_framing.sv

build/decimal.vvp: $(RTL) tb_ouch_decimal64.sv rtl.f | build
	$(IVERILOG) $(VFLAGS) -s tb_ouch_decimal64 -o $@ -f rtl.f tb_ouch_decimal64.sv

lint:
	$(VERILATOR) --lint-only -Wall -Wno-UNUSEDSIGNAL --top-module $(TOP) -f rtl.f
	$(VERILATOR) --lint-only -Wall -Wno-UNUSEDSIGNAL --top-module $(TOP) -GREPLAY_HISTORY_DEPTH=5 -f rtl.f

synth: | build
	$(YOSYS) -Q -q -T -m slang -l build/synth.log -p 'read_slang --top $(TOP) $(RTL); synth -top $(TOP); check -assert; stat; write_json build/session-generic.json'
	$(YOSYS) -Q -q -T -m slang -l build/replay-synth.log -p 'read_slang --top $(TOP) -G REPLAY_HISTORY_DEPTH=5 $(RTL); synth -top $(TOP); check -assert; stat; write_json build/replay-generic.json'

.PHONY: formal-replay-1 formal-replay-3
formal-replay: formal-replay-1 formal-replay-3

formal-replay-1 formal-replay-3: formal-replay-%: | build
	$(YOSYS) -Q -q -T -m slang -l build/replay-formal-$*.log -p 'read_slang --formal -D FORMAL --top formal_ouch_replay -G DEPTH=$* ouch_wire_pkg.sv ouch_replay_window.sv formal_ouch_replay.sv; prep -top formal_ouch_replay; memory_map; flatten; async2sync; chformal -lower; opt; sat -seq 12 -set-assumes -prove-asserts -verify'

timing: | build
	$(YOSYS) -Q -q -T -m slang -l build/synth-ecp5.log -p 'read_slang --top $(TOP) $(RTL); synth_ecp5 -top $(TOP) -json build/session-ecp5.json; check -assert; stat'
	$(NEXTPNR) --85k --package CABGA381 --speed 8 --threads 1 --out-of-context \
		--freq $(ROUTE_FREQ) --seed $(ROUTE_SEED) --timing-allow-fail \
		--json build/session-ecp5.json --report build/session-route.json --log build/nextpnr.log --quiet

wave-vcd: build/session.vvp
	$(PYTHON) -B gen_ouch_vectors.py --output build/wave --slots 8 --operations $(OPERATIONS) --seed $(SEED)
	$(VVP) $< +vectors=build/wave +waves=build/wave/session.vcd
	$(PYTHON) -B ouch_audit.py check-tx build/wave/hardware_tx.hex build/wave/tx.txt

waveform: wave-vcd replay-waveform
	$(PYTHON) -B ../scripts/render_vcd_waveform.py build/wave/session.vcd waveform.svg \
		--start 13280 --end 15000 --title 'OUCH replay catches up before the live cursor advances' \
		--signal 'tb_ouch_order_session.replaying=Replay Active' \
		--signal 'tb_ouch_order_session.next_sequence=Next Live Sequence' \
		--signal 'tb_ouch_order_session.m_notice_valid=Notice Valid' \
		--signal 'tb_ouch_order_session.m_tx_valid=Client TX Valid' \
		--signal 'tb_ouch_order_session.m_tx_last=Client TX Last' \
		--signal 'tb_ouch_order_session.s_rx_valid=Server RX Valid'

replay-waveform: build/replay-5.vvp
	$(PYTHON) -B gen_replay_vectors.py --output build/replay-wave --depth 5 --seed 0x9c0045 --operations 1
	$(VVP) $< +vectors=build/replay-wave +stall_seed=17235 +waves=build/replay-wave/session.vcd
	$(PYTHON) -B ouch_audit.py check-tx build/replay-wave/hardware_tx.hex build/replay-wave/tx.txt
	$(PYTHON) -B ../scripts/render_vcd_waveform.py build/replay-wave/session.vcd replay-integrity-waveform.svg \
		--start 8200 --end 16050 --title 'OUCH replay divergence retains the committed cursor and verification fence' \
		--signal 'tb_ouch_order_session.transport_up=TCP Link Up' \
		--signal 'tb_ouch_order_session.replaying=Replaying' \
		--signal 'tb_ouch_order_session.dut.replay_fenced=Verification Required' \
		--signal 'tb_ouch_order_session.dut.socket_cursor=Socket Sequence' \
		--signal 'tb_ouch_order_session.next_sequence=Committed Next' \
		--signal 'tb_ouch_order_session.last_fault=Fault Code' \
		--signal 'tb_ouch_order_session.close_transport=Close TCP Link'

clean:
	rm -rf build
