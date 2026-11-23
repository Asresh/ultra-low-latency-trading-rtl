PYTHON ?= python3
IVERILOG ?= iverilog
VVP ?= vvp
VERILATOR ?= verilator
YOSYS ?= yosys
SEED ?= 4101
BURSTS ?= 72
BUILD := build
RTL := $(shell cat rtl.f)
HEADERS := frontend_codes.svh frontend_words.svh
FLAGS := -g2012 -Wall -I.
UNITS := $(addprefix $(BUILD)/,tb_frontend_pair_ram.vvp tb_eth_packet_arbiter.vvp tb_eth_counter_bank.vvp tb_eth_config_csr.vvp tb_eth_reset_recovery.vvp)

.PHONY: all test test-main test-strict test-default test-units test-python test-replay lint synth clean
all: test

test: test-main test-strict test-default test-units test-python test-replay

test-main: $(BUILD)/tb_ethernet_udp_frontend.vvp
	$(PYTHON) -B net_scenarios.py --directory $(BUILD)/standard --seed $(SEED) --bursts $(BURSTS)
	$(VVP) $< +vectors=$(BUILD)/standard
	$(PYTHON) -B net_scenarios.py --directory $(BUILD)/another --seed 9717 --bursts $(BURSTS) --fault-samples 90
	$(VVP) $< +vectors=$(BUILD)/another

test-strict: $(BUILD)/strict.vvp
	$(PYTHON) -B net_scenarios.py --directory $(BUILD)/strict --seed 4414 --bursts 40 --strict-udp
	$(VVP) $< +vectors=$(BUILD)/strict

test-default: $(BUILD)/default.vvp
	$(PYTHON) -B net_scenarios.py --directory $(BUILD)/default --seed 9017 --bursts 40 --default-geometry
	$(VVP) $< +vectors=$(BUILD)/default

test-replay: $(BUILD)/tb_eth_capture_replay.vvp $(BUILD)/replay-strict.vvp
	$(PYTHON) -B capture_replay.py --synthetic --directory $(BUILD)/replay
	$(VVP) $(BUILD)/tb_eth_capture_replay.vvp +vectors=$(BUILD)/replay
	$(PYTHON) -B capture_replay.py --synthetic --strict --directory $(BUILD)/replay-strict
	$(VVP) $(BUILD)/replay-strict.vvp +vectors=$(BUILD)/replay-strict

test-python:
	$(PYTHON) -B -m unittest discover -q

test-units: $(UNITS)
	@set -e; for test in $(UNITS); do $(VVP) $$test; done

$(BUILD)/tb_%.vvp: tb_%.sv $(RTL) $(HEADERS) rtl.f
	mkdir -p $(BUILD)
	$(IVERILOG) $(FLAGS) -s tb_$* -o $@ -f rtl.f $<

$(BUILD)/strict.vvp: tb_ethernet_udp_frontend.sv $(RTL) $(HEADERS) rtl.f
	mkdir -p $(BUILD)
	$(IVERILOG) $(FLAGS) -DSTRICT_UDP -s tb_ethernet_udp_frontend -o $@ -f rtl.f $<

$(BUILD)/replay-strict.vvp: tb_eth_capture_replay.sv $(RTL) $(HEADERS) rtl.f
	mkdir -p $(BUILD)
	$(IVERILOG) $(FLAGS) -DSTRICT_UDP -s tb_eth_capture_replay -o $@ -f rtl.f $<

$(BUILD)/default.vvp: tb_ethernet_udp_frontend.sv $(RTL) $(HEADERS) rtl.f
	mkdir -p $(BUILD)
	$(IVERILOG) $(FLAGS) -DDEFAULT_GEOMETRY -s tb_ethernet_udp_frontend -o $@ -f rtl.f $<

lint:
	$(VERILATOR) --lint-only --Wall -I. --top-module ethernet_udp_frontend -f rtl.f
	$(VERILATOR) --lint-only --Wall -I. --top-module eth_config_csr -f rtl.f

synth:
	mkdir -p $(BUILD)
	$(YOSYS) -ql $(BUILD)/synth.log -p 'read_verilog -sv -I. $(RTL); synth -top ethernet_udp_frontend -noabc; check -assert; stat; write_json $(BUILD)/frontend.json'
	$(YOSYS) -ql $(BUILD)/csr-synth.log -p 'read_verilog -sv eth_config_csr.sv; synth -top eth_config_csr -noabc; check -assert; stat'
	tail -n 23 $(BUILD)/synth.log

clean:
	rm -rf $(BUILD) __pycache__ obj_dir
