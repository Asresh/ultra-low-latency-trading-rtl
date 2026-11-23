PYTHON ?= python3
IVERILOG ?= iverilog
VVP ?= vvp
VERILATOR ?= verilator
YOSYS ?= yosys
BUILD := build
TOP := host_dma_queues
RTL := $(shell cat rtl.f)
HEADERS := dma_protocol.svh
MEMORY_MODEL := tb_dma_memory.sv

.PHONY: all test regress icarus test-host test-components test-integration lint synth clean
all: test
regress icarus: test
test: test-host test-components test-integration

test-host: | $(BUILD)
	$(PYTHON) -B -m unittest discover -v
	$(PYTHON) -B dma_workload.py --seed 8088 --requests 96 --depth 8 --output $(BUILD)/ledger.json

test-components: $(BUILD)/tb_dma_axi64.vvp $(BUILD)/tb_dma_axi64_max.vvp $(BUILD)/tb_dma_queue_table.vvp $(BUILD)/tb_dma_crc_irq.vvp
	@set -e; for bench in $^; do $(VVP) "$$bench"; done

test-integration: $(BUILD)/tb_host_dma_queues.vvp $(BUILD)/tb_dma_contention.vvp
	$(VVP) $(BUILD)/tb_host_dma_queues.vvp +capture=$(BUILD)
	$(PYTHON) -B audit_directed_capture.py $(BUILD)
	$(VVP) $(BUILD)/tb_dma_contention.vvp

$(BUILD):
	mkdir -p $@

$(BUILD)/tb_dma_axi64_max.vvp: tb_dma_axi64.sv $(RTL) $(HEADERS) rtl.f | $(BUILD)
	$(IVERILOG) -g2012 -I . -s tb_dma_axi64 -Ptb_dma_axi64.MAX_BURST=16 -o $@ -f rtl.f $<

$(BUILD)/tb_host_dma_queues.vvp $(BUILD)/tb_dma_contention.vvp: $(BUILD)/tb_%.vvp: tb_%.sv $(MEMORY_MODEL) $(RTL) $(HEADERS) rtl.f | $(BUILD)
	$(IVERILOG) -g2012 -I . -s tb_$* -o $@ -f rtl.f $< $(MEMORY_MODEL)

$(BUILD)/tb_%.vvp: tb_%.sv $(RTL) $(HEADERS) rtl.f | $(BUILD)
	$(IVERILOG) -g2012 -I . -s tb_$* -o $@ -f rtl.f $<

lint:
	$(VERILATOR) --lint-only -Wall -Wno-PINCONNECTEMPTY -I. --top-module $(TOP) -f rtl.f

synth: | $(BUILD)
	$(YOSYS) -Q -q -l $(BUILD)/synth.log -m slang -p 'read_slang -I. --top $(TOP) $(RTL); synth -top $(TOP) -noabc; check -assert; stat'
	tail -n 22 $(BUILD)/synth.log

clean:
	rm -rf $(BUILD)
