PYTHON ?= python3
IVERILOG ?= iverilog
VVP ?= vvp
VERILATOR ?= verilator
YOSYS ?= yosys
SBY ?= sby
BUILD ?= build
CYCLES ?= 3500
SEED ?= 0x161cee
TOP := credit_order_scheduler
TB := tb_credit_order_scheduler
RUN := $(PYTHON) -B scheduler_regress.py --iverilog "$(IVERILOG)" --vvp "$(VVP)" --seed $(SEED)

.PHONY: all test lint synth formal waveform clean
all: test

test: lease-test
	$(PYTHON) -B -m unittest -v test_scheduler
	$(RUN) --build $(BUILD)/matrix --cycles $(CYCLES)

lint: lease-lint
	$(VERILATOR) --lint-only -Wall --top-module $(TOP) -f rtl.f
	$(VERILATOR) --lint-only -Wall --top-module $(TOP) -GCLASSES=1 -GDEPTH=1 -GCREDIT_W=3 -f rtl.f
	$(VERILATOR) --lint-only -Wall --top-module $(TOP) -GCLASSES=5 -GDEPTH=3 -GCREDIT_W=6 -f rtl.f

synth: lease-synth
	mkdir -p $(BUILD)
	$(YOSYS) -Q -m slang -l $(BUILD)/synth.log -q -p 'read_slang --top $(TOP) -F rtl.f; synth -top $(TOP); check -assert; stat; write_json $(BUILD)/scheduler.json'

formal: lease-formal
	$(SBY) -f --prefix $(BUILD)/scheduler scheduler.sby

waveform: lease-waveform
	$(RUN) --build $(BUILD)/wave --profile default --cycles 0 --wave $(BUILD)/egress.vcd
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/egress.vcd waveform.svg \
		--start 20 --end 99 --title 'Credit scheduler · Held offer spends credit only on transfer' \
		--signal '$(TB).clk=Clock' \
		--signal '$(TB).s_valid=Ingress valid' \
		--signal '$(TB).s_tag=Ingress tag' \
		--signal '$(TB).m_class=Egress lane' \
		--signal '$(TB).m_valid=Egress valid' \
		--signal '$(TB).m_ready=Egress ready' \
		--signal '$(TB).m_tag=Egress tag' \
		--signal '$(TB).ctl_ready=Control ready' \
		--signal '$(TB).ctl_op=Control opcode' \
		--signal '$(TB).ctl_amount=Control amount' \
		--signal '$(TB).report_class=Report lane' \
		--signal '$(TB).report_status=Report status' \
		--signal '$(TB).report_available=Free credit' \
		--signal '$(TB).report_inflight=In-flight credit'

clean:
	rm -rf $(BUILD)

LEASE_RTL := leased_credit_scheduler.sv credit_order_scheduler.sv
LEASE_TB := tb_leased_credit_scheduler
.PHONY: lease-test lease-lint lease-synth lease-formal lease-waveform

$(BUILD)/leases.vvp: $(LEASE_TB).sv $(LEASE_RTL)
	mkdir -p $(BUILD)
	$(IVERILOG) -g2012 -s $(LEASE_TB) -o $@ $^

lease-test: $(BUILD)/leases.vvp
	$(VVP) $<

lease-lint:
	$(VERILATOR) --lint-only -Wall --top-module leased_credit_scheduler $(LEASE_RTL)
	$(VERILATOR) --lint-only -Wall --top-module leased_credit_scheduler -GCLASSES=1 -GLEASE_SLOTS=1 -GGEN_W=1 $(LEASE_RTL)

lease-synth:
	mkdir -p $(BUILD)
	$(YOSYS) -Q -q -l $(BUILD)/lease-synth.log -m slang -p 'read_slang --top leased_credit_scheduler $(LEASE_RTL); synth -top leased_credit_scheduler; check -assert; stat'

lease-formal:
	$(SBY) -f --prefix $(BUILD)/leases-formal leases.sby

lease-waveform: $(BUILD)/leases.vvp
	$(VVP) $< +wave=$(BUILD)/leases.vcd
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/leases.vcd lease-waveform.svg \
		--start 40 --end 220 --title 'Credit lease · An offered token becomes active on transfer' \
		--signal '$(LEASE_TB).clk=Clock' --signal '$(LEASE_TB).m_valid=Order valid' \
		--signal '$(LEASE_TB).m_ready=Order ready' --signal '$(LEASE_TB).m_lease=Offered token' \
		--signal '$(LEASE_TB).ctl_valid=Control valid' --signal '$(LEASE_TB).ctl_lease=Returned token' \
		--signal '$(LEASE_TB).report_valid=Report valid' --signal '$(LEASE_TB).report_status=Report status' \
		--signal '$(LEASE_TB).lease_active=Active slots'
