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

.PHONY: all test test-main test-strict test-default test-units test-python test-replay test-arp test-route lint synth clean waveform-arp
all: test

test: test-main test-strict test-default test-units test-python test-replay test-arp test-route

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

test-arp: $(BUILD)/tb_eth_arp_endpoint.vvp $(BUILD)/tb_eth_arp_deadline.vvp
	$(VVP) $< +capture=$(BUILD)/arp-capture.hex
	$(PYTHON) -B arp_reference.py $(BUILD)/arp-capture.hex
	$(VVP) $(BUILD)/tb_eth_arp_deadline.vvp

test-route: $(BUILD)/route-8.vvp $(BUILD)/route-11.vvp
	@set -e; for rows in 8 11; do \
		$(PYTHON) -B route_vectors.py $(BUILD)/route-$$rows.hex --rows $$rows --seed $$((40402+rows)); \
		$(VVP) $(BUILD)/route-$$rows.vvp +tape=$(BUILD)/route-$$rows.hex; \
	done

$(BUILD)/route-%.vvp: tb_eth_ipv4_route.sv eth_ipv4_route.sv
	mkdir -p $(BUILD)
	$(IVERILOG) $(FLAGS) -s tb_eth_ipv4_route -Ptb_eth_ipv4_route.ROWS=$* -o $@ eth_ipv4_route.sv $<

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
	$(VERILATOR) --lint-only --Wall -I. --top-module eth_arp_endpoint -f rtl.f
	$(VERILATOR) --lint-only --Wall -I. --top-module eth_ipv4_route -f rtl.f

synth:
	mkdir -p $(BUILD)
	$(YOSYS) -ql $(BUILD)/synth.log -p 'read_verilog -sv -I. $(RTL); synth -top ethernet_udp_frontend -noabc; check -assert; stat; write_json $(BUILD)/frontend.json'
	$(YOSYS) -ql $(BUILD)/csr-synth.log -p 'read_verilog -sv eth_config_csr.sv; synth -top eth_config_csr -noabc; check -assert; stat'
	$(YOSYS) -ql $(BUILD)/arp-synth.log -p 'read_verilog -sv eth_arp_resolver.sv eth_arp_wire.sv eth_arp_endpoint.sv; synth -top eth_arp_endpoint -noabc; check -assert; stat'
	$(YOSYS) -ql $(BUILD)/route-synth.log -p 'read_verilog -sv eth_ipv4_route.sv; synth -top eth_ipv4_route -noabc; check -assert; stat'
	tail -n 23 $(BUILD)/synth.log

clean:
	rm -rf $(BUILD) __pycache__ obj_dir

.PHONY: waveform
waveform: $(BUILD)/tb_eth_packet_arbiter.vvp waveform-arp
	$(VVP) $< +wave=$(BUILD)/packet-arbiter.vcd
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/packet-arbiter.vcd waveform.svg \
		--start 20 --end 155 --title 'Ethernet TX · Packet lock during output backpressure' \
		--signal 'tb_eth_packet_arbiter.clk=Clock' \
		--signal 'tb_eth_packet_arbiter.in_valid=Lane valid mask' \
		--signal 'tb_eth_packet_arbiter.in_ready=Lane ready mask' \
		--signal 'tb_eth_packet_arbiter.out_valid=MAC valid' \
		--signal 'tb_eth_packet_arbiter.out_ready=MAC ready' \
		--signal 'tb_eth_packet_arbiter.out_lane=Selected lane' \
		--signal 'tb_eth_packet_arbiter.dut.locked=Packet locked' \
		--signal 'tb_eth_packet_arbiter.out_data=MAC beat'

waveform-arp: $(BUILD)/tb_eth_arp_endpoint.vvp
	$(VVP) $< +capture=$(BUILD)/arp-wave.hex +wave=$(BUILD)/arp.vcd
	$(PYTHON) -B arp_reference.py $(BUILD)/arp-wave.hex
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/arp.vcd arp-waveform.svg \
		--start 1370 --end 1785 --title 'ARP · A transmitted probe completes two waiting lookups' \
		--signal 'tb_eth_arp_endpoint.clk=Clock' \
		--signal 'tb_eth_arp_endpoint.m_mac_valid=MAC TX valid' \
		--signal 'tb_eth_arp_endpoint.m_mac_ready=MAC TX ready' \
		--signal 'tb_eth_arp_endpoint.m_mac_last=MAC TX last' \
		--signal 'tb_eth_arp_endpoint.probes_sent=Transmitted probes' \
		--signal 'tb_eth_arp_endpoint.replies_learned=Accepted neighbors' \
		--signal 'tb_eth_arp_endpoint.m_valid=Lookup result valid' \
		--signal 'tb_eth_arp_endpoint.m_ready=Lookup result ready' \
		--signal 'tb_eth_arp_endpoint.m_cookie=Completed cookie'
