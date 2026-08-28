# Local pipeline runs against the checked-in samples/. CI uses the same Python
# but fetches live extracts (scripts/fetch_region.sh).
#
#   make selftest              gate 5 (merge honesty), no data needed
#   make build R=switzerland   build one sample region -> build/switzerland.pmtiles
#   make verify R=switzerland  run gates 2-5 on it
#   make join                  tile-join a few sample regions -> build/europe.pmtiles
#   make all                   build + verify every sample region

R ?= switzerland
BUILD := build
SAMPLES := samples

REGIONS := $(patsubst $(SAMPLES)/%-rail.osm.pbf,%,$(wildcard $(SAMPLES)/*-rail.osm.pbf))

.PHONY: selftest build verify join all clean

selftest:
	python3 pipeline/verify.py --self-test

$(BUILD):
	mkdir -p $(BUILD)

build: $(BUILD)
	python3 pipeline/build_region.py $(R) $(BUILD)/$(R).pmtiles $(SAMPLES)/$(R)-rail.osm.pbf

verify:
	python3 pipeline/verify.py $(BUILD)/$(R).pmtiles

all: $(BUILD)
	@for r in $(REGIONS); do \
	  echo "=== $$r ==="; \
	  python3 pipeline/build_region.py $$r $(BUILD)/$$r.pmtiles $(SAMPLES)/$$r-rail.osm.pbf || exit 1; \
	  python3 pipeline/verify.py $(BUILD)/$$r.pmtiles || exit 1; \
	done

join: $(BUILD)
	tile-join -o $(BUILD)/europe.pmtiles --no-tile-size-limit --force $(BUILD)/*.pmtiles
	ls -la $(BUILD)/europe.pmtiles

clean:
	rm -rf $(BUILD)
