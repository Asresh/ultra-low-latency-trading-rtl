IVERILOG ?= iverilog
VVP ?= vvp
YOSYS ?= yosys
NEXTPNR_ECP5 ?= nextpnr-ecp5
FPGA_MHZ ?= 100
FPGA_SEED ?= 1

TOP := pretrade_risk_gateway
RTL := rtl/$(TOP).sv
TB := tb/$(TOP)_tb.sv
SIM := build/$(TOP).vvp

.PHONY: all test synth timing clean
all: test

test: $(SIM)
	$(VVP) $(SIM)

$(SIM): $(RTL) $(TB) Makefile
	mkdir -p build
	$(IVERILOG) -g2012 -Wall -Wno-sensitivity-entire-array -s $(TOP)_tb -o $(SIM) $(RTL) $(TB)

synth:
	mkdir -p build
	$(YOSYS) -Q -T -l build/synth.log -p 'read_verilog -sv $(RTL); hierarchy -check -top $(TOP); synth -top $(TOP); check -assert; stat'

timing:
	mkdir -p build
	$(YOSYS) -Q -T -q -l build/synth-ecp5.log -p 'read_verilog -sv $(RTL); synth_ecp5 -top $(TOP) -json build/risk_ecp5.json; check -assert'
	$(NEXTPNR_ECP5) --85k --package CABGA381 --speed 8 --out-of-context \
		--freq $(FPGA_MHZ) --seed $(FPGA_SEED) --json build/risk_ecp5.json \
		--report build/risk_ecp5_report.json --log build/nextpnr-ecp5.log --quiet

clean:
	rm -rf build
