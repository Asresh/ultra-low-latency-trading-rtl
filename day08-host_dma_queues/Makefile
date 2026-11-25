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

.PHONY: waveform wave-vcd waveform-readback all test regress icarus test-host test-components test-integration test-readback lint synth clean
all: test
regress icarus: test
test: test-host test-components test-integration test-readback

test-host: | $(BUILD)
	$(PYTHON) -B -m unittest discover -v
	$(PYTHON) -B dma_workload.py --seed 8088 --requests 96 --depth 8 --output $(BUILD)/ledger.json

test-components: $(BUILD)/tb_dma_axi64.vvp $(BUILD)/tb_dma_axi64_max.vvp $(BUILD)/tb_dma_queue_table.vvp $(BUILD)/tb_dma_crc_irq.vvp
	@set -e; for bench in $^; do $(VVP) "$$bench"; done

test-integration: $(BUILD)/tb_host_dma_queues.vvp $(BUILD)/tb_dma_contention.vvp
	$(VVP) $(BUILD)/tb_host_dma_queues.vvp +capture=$(BUILD)
	$(PYTHON) -B audit_directed_capture.py $(BUILD)
	$(VVP) $(BUILD)/tb_dma_contention.vvp

test-readback: $(BUILD)/tb_host_dma_queues.vvp
	mkdir -p $(BUILD)/readback
	$(VVP) $< +readback +capture=$(BUILD)/readback
	$(PYTHON) -B audit_dma_readback.py $(BUILD)/readback

waveform-readback: $(BUILD)/tb_host_dma_queues.vvp ../scripts/render_vcd_waveform.py
	mkdir -p $(BUILD)/readback-wave
	$(VVP) $< +readback +capture=$(BUILD)/readback-wave +waves=$(BUILD)/readback-wave/readback.vcd
	$(PYTHON) -B audit_dma_readback.py $(BUILD)/readback-wave
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/readback-wave/readback.vcd readback_waveform.svg \
		--start 1090 --end 1855 --title 'Verified PUSH waits for payload B, readback, and CQ B' \
		--signal 'tb_host_dma_queues.dut.state=DMA worker state' \
		--signal 'tb_host_dma_queues.block_write_response=Memory holds B' \
		--signal 'tb_host_dma_queues.bvalid=AXI B valid' \
		--signal 'tb_host_dma_queues.bready=AXI B ready' \
		--signal 'tb_host_dma_queues.arvalid=AXI AR valid' \
		--signal 'tb_host_dma_queues.rvalid=AXI R valid' \
		--signal 'tb_host_dma_queues.rlast=AXI R last' \
		--signal 'tb_host_dma_queues.awvalid=AXI AW valid' \
		--signal 'tb_host_dma_queues.inspect_cq_tail=Queue 0 CQ tail' \
		--signal 'tb_host_dma_queues.irq=IRQ'

$(BUILD):
	mkdir -p $@

$(BUILD)/tb_dma_axi64_max.vvp: tb_dma_axi64.sv $(RTL) $(HEADERS) rtl.f | $(BUILD)
	$(IVERILOG) -g2012 -I . -s tb_dma_axi64 -Ptb_dma_axi64.MAX_BURST=16 -o $@ -f rtl.f $<

$(BUILD)/tb_host_dma_queues.vvp $(BUILD)/tb_dma_contention.vvp: $(BUILD)/tb_%.vvp: tb_%.sv $(MEMORY_MODEL) $(RTL) $(HEADERS) rtl.f | $(BUILD)
	$(IVERILOG) -g2012 -I . -s tb_$* -o $@ -f rtl.f $< $(MEMORY_MODEL)

$(BUILD)/tb_host_dma_queues.vvp: tb_dma_readback.svh

$(BUILD)/tb_%.vvp: tb_%.sv $(RTL) $(HEADERS) rtl.f | $(BUILD)
	$(IVERILOG) -g2012 -I . -s tb_$* -o $@ -f rtl.f $<

lint:
	$(VERILATOR) --lint-only -Wall -Wno-PINCONNECTEMPTY -I. --top-module $(TOP) -f rtl.f

synth: | $(BUILD)
	$(YOSYS) -Q -q -l $(BUILD)/synth.log -m slang -p 'read_slang -I. --top $(TOP) $(RTL); synth -top $(TOP) -noabc; check -assert; stat'
	tail -n 22 $(BUILD)/synth.log

wave-vcd: $(BUILD)/tb_host_dma_queues.vvp
	mkdir -p $(BUILD)/wave
	$(VVP) $< +capture=$(BUILD)/wave +waves=$(BUILD)/wave/dma.vcd
	$(PYTHON) -B audit_directed_capture.py $(BUILD)/wave

waveform: wave-vcd
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/wave/dma.vcd waveform.svg \
		--start 8990 --end 9195 --title 'Completion publication waits for the AXI write response' \
		--signal 'tb_host_dma_queues.clk=Clock' \
		--signal 'tb_host_dma_queues.block_write_response=Memory Holds B' \
		--signal 'tb_host_dma_queues.wvalid=AXI W Valid' \
		--signal 'tb_host_dma_queues.wready=AXI W Ready' \
		--signal 'tb_host_dma_queues.wlast=AXI W Last' \
		--signal 'tb_host_dma_queues.bvalid=AXI B Valid' \
		--signal 'tb_host_dma_queues.bready=AXI B Ready' \
		--signal 'tb_host_dma_queues.inspect_cq_tail=Queue 0 CQ Tail'

clean:
	rm -rf $(BUILD)
