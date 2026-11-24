TOP := drop_copy_reconciliation
TB := tb_drop_copy_reconciliation
BUILD := build
PYTHON ?= python3
IVERILOG ?= iverilog
VVP ?= vvp
VERILATOR ?= verilator
YOSYS ?= yosys
SBY ?= sby
RTL := $(shell cat rtl.f)
HEADERS := reconcile_defs.svh

.PHONY: all test lint synth formal clean wave-vcd waveform
all: test

$(BUILD):
	mkdir -p $@

$(BUILD)/reconcile-%.vvp: $(RTL) $(HEADERS) $(TB).sv rtl.f | $(BUILD)
	$(IVERILOG) -g2012 -I. -s $(TB) -P $(TB).SETS=$* -o $@ -f rtl.f $(TB).sv

test: $(BUILD)/reconcile-1.vvp $(BUILD)/reconcile-4.vvp $(BUILD)/reconcile-8.vvp
	$(PYTHON) -B -m unittest discover -v
	@set -e; for depth in 1 4 8; do \
		$(PYTHON) -B check_drop_copy.py generate $(BUILD)/sets-$$depth --sets $$depth --seed $$depth; \
		$(VVP) $(BUILD)/reconcile-$$depth.vvp +vectors=$(BUILD)/sets-$$depth; \
		$(PYTHON) -B check_drop_copy.py check $(BUILD)/sets-$$depth; \
	done

lint:
	$(VERILATOR) --lint-only -Wall --top-module $(TOP) -f rtl.f

synth: | $(BUILD)
	$(YOSYS) -Q -q -m slang -l $(BUILD)/synth.log -p 'read_slang -I. --top $(TOP) $(RTL); synth -top $(TOP) -noabc; check -assert; stat'
	tail -n 22 $(BUILD)/synth.log

formal: | $(BUILD)
	$(SBY) -f --prefix $(BUILD)/formal drop_copy.sby

wave-vcd: $(BUILD)/reconcile-4.vvp
	$(PYTHON) -B check_drop_copy.py generate $(BUILD)/wave --cycles 0
	$(VVP) $< +vectors=$(BUILD)/wave +waves=$(BUILD)/wave/reconcile.vcd
	$(PYTHON) -B check_drop_copy.py check $(BUILD)/wave

waveform: wave-vcd
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/wave/reconcile.vcd waveform.svg \
		--start 25 --end 115 --title 'Drop-copy match and same-lane conflict' \
		--signal 'tb_drop_copy_reconciliation.clk=Clock' \
		--signal 'tb_drop_copy_reconciliation.s_primary_valid=Primary Valid' \
		--signal 'tb_drop_copy_reconciliation.s_copy_valid=Copy Valid' \
		--signal 'tb_drop_copy_reconciliation.s_copy_ready=Copy Ready' \
		--signal 'tb_drop_copy_reconciliation.m_valid=Report Valid' \
		--signal 'tb_drop_copy_reconciliation.m_ready=Report Ready' \
		--signal 'tb_drop_copy_reconciliation.m_status=Report Status' \
		--signal 'tb_drop_copy_reconciliation.m_diff=Field Diff' \
		--signal 'tb_drop_copy_reconciliation.pending_count=Pending Keys'

clean:
	rm -rf $(BUILD)
