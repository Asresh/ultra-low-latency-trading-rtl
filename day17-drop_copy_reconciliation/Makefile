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

.PHONY: test-partitions lint-partitions synth-partitions formal-partitions partition-waveform

PARTITION_TB := tb_drop_copy_partitions
PARTITION_TOP := drop_copy_partitions
PARTITION_TESTS := test-partitions-1 test-partitions-3 test-partitions-5
PARTITION_SETS_1 := 1
PARTITION_SETS_3 := 4
PARTITION_SETS_5 := 8

.PHONY: $(PARTITION_TESTS)
test: test-partitions
lint: lint-partitions
synth: synth-partitions
formal: formal-partitions
waveform: partition-waveform

test-partitions: $(PARTITION_TESTS)

$(PARTITION_TESTS): test-partitions-%: $(BUILD)/partitions-%.vvp
	$(PYTHON) -B check_partitions.py generate $(BUILD)/partition-$* --namespaces $* --sets $(PARTITION_SETS_$*) --seed $*
	$(VVP) $< +vectors=$(BUILD)/partition-$*
	$(PYTHON) -B check_partitions.py check $(BUILD)/partition-$*

$(BUILD)/partitions-%.vvp: $(RTL) $(HEADERS) $(PARTITION_TB).sv rtl.f | $(BUILD)
	$(IVERILOG) -g2012 -I. -s $(PARTITION_TB) -P $(PARTITION_TB).NAMESPACES=$* \
		-P $(PARTITION_TB).SETS=$(PARTITION_SETS_$*) -o $@ -f rtl.f $(PARTITION_TB).sv

lint-partitions:
	$(VERILATOR) --lint-only -Wall --top-module $(PARTITION_TOP) -f rtl.f
	$(VERILATOR) --lint-only -Wall --top-module $(PARTITION_TOP) -GNAMESPACES=1 -GSETS=1 -f rtl.f

synth-partitions: | $(BUILD)
	$(YOSYS) -Q -q -m slang -l $(BUILD)/partitions-synth.log -p 'read_slang -I. --top $(PARTITION_TOP) $(RTL); synth -top $(PARTITION_TOP) -noabc; check -assert; stat'

formal-partitions: | $(BUILD)
	$(YOSYS) -Q -q -m slang -l $(BUILD)/partitions-formal.log -p 'read_slang --formal -I. --top formal_drop_copy_partitions $(RTL) formal_drop_copy_partitions.sv; prep -top formal_drop_copy_partitions; memory_map; flatten; async2sync; chformal -lower; opt; sat -seq 8 -set-at 1 rst 1 -prove-asserts -verify'

partition-waveform: $(BUILD)/partitions-3.vvp
	$(PYTHON) -B check_partitions.py generate $(BUILD)/partition-wave --cycles 0
	$(VVP) $< +vectors=$(BUILD)/partition-wave +waves=$(BUILD)/partition-wave/session.vcd
	$(PYTHON) -B check_partitions.py check $(BUILD)/partition-wave
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/partition-wave/session.vcd partition-waveform.svg \
		--start 17 --end 260 --title 'Independent reconciliation and a targeted namespace restart' \
		--signal 'tb_drop_copy_partitions.s_primary_valid=Primary valid' \
		--signal 'tb_drop_copy_partitions.s_copy_valid=Copy valid' \
		--signal 'tb_drop_copy_partitions.m_ready=Audit ready' \
		--signal 'tb_drop_copy_partitions.m_valid=Audit valid' \
		--signal 'tb_drop_copy_partitions.m_namespace=Report namespace' \
		--signal 'tb_drop_copy_partitions.m_status=Report code' \
		--signal 'tb_drop_copy_partitions.s_epoch_valid=Epoch request valid' \
		--signal 'tb_drop_copy_partitions.namespace_incidents=Incident bitmap' \
		--signal 'tb_drop_copy_partitions.active_epochs=Packed epochs'
