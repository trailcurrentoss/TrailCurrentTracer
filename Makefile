# Tracer OS
#
# `make help` lists what is implemented. The few remaining unimplemented
# targets (lint) fail loudly rather than pretending to succeed.

# The board's address is NOT set here. It belongs to the bench the board sits
# on, so it lives in scripts/dev.env (gitignored) — see scripts/dev.env.example.
# The dev targets resolve it through scripts/dev-env.sh.

OVERLAY_SRC := image/overlays/tracer-gt911-overlay.dts
OVERLAY_BIN := image/overlays/tracer-gt911.dtbo
VENDOR_5B   := image/vendor/3.5HDMI_E_DTBO/waveshare-35dpi-5b.dtbo

.PHONY: help overlays verify-overlays splash clean image dev dev-mock dev-kiosk dev-shot dev-stop dev-logs dev-provision dev-check dev-autostart dev-no-autostart mock icons test lint flash

help:
	@echo "Implemented:"
	@echo "  overlays         Build tracer-gt911.dtbo from source"
	@echo "  verify-overlays  Prove the derived overlay matches vendor except for the 0x14 node"
	@echo "  splash           Render the 640x480 boot splash TGA"
	@echo "  image            Build the OS image (needs sudo). See docs/building.md."
	@echo "                   Installs tracerd + tracer-ui and enables both units:"
	@echo "                   a flashed image boots to the splash, then the launcher."
	@echo "  clean            Remove build artifacts"
	@echo ""
	@echo "Run it:"
	@echo "  mock             Run daemon + GUI locally (no hardware)  http://127.0.0.1:8710/?dev"
	@echo "  dev              Deploy to the live board and restart tracerd"
	@echo "  dev-mock         ...in mock mode"
	@echo "  dev-kiosk        Deploy and show it on the board's panel"
	@echo "  dev-shot         Screenshot the panel -> ./panel.png"
	@echo "  dev-autostart    Deploy and start Tracer at boot on the dev board"
	@echo "  dev-no-autostart Stop starting it at boot"
	@echo "  dev-stop         Stop the dev daemon on the board"
	@echo "  dev-logs         Tail its log"
	@echo "  dev-provision    Install the image's SYSTEM files on the dev board"
	@echo "                   (polkit, sudoers, locale helper). Needs sudo THERE."
	@echo "  dev-check        Verify the board matches the image. Changes nothing."
	@echo "  icons            Regenerate the offline Ionicons subset"
	@echo ""
	@echo "  test             Run daemon unit tests + UI syntax check"
	@echo ""
	@echo "Not implemented:"
	@echo "  lint"

overlays: $(OVERLAY_BIN)

$(OVERLAY_BIN): $(OVERLAY_SRC)
	dtc -@ -I dts -O dtb -o $@ $< 2>&1
	@echo "built $@"

# Guards the one hand-derived binary in the tree. If a future edit drifts the
# GT911's interrupt, GPIO, or touchscreen-size values away from the known-good
# vendor blob, this fails — which is exactly when touch would break on hardware.
verify-overlays: $(OVERLAY_BIN)
	@echo "== touch nodes: vendor -5b =="
	@dtc -I dtb -O dts $(VENDOR_5B) 2>/dev/null | grep -oE '^\s+(ft6236|gt911)@[0-9a-f]+' | tr -d ' \t'
	@echo "== touch nodes: tracer-gt911 (expect only gt911@5d) =="
	@dtc -I dtb -O dts $(OVERLAY_BIN) 2>/dev/null | grep -oE '^\s+(ft6236|gt911)@[0-9a-f]+' | tr -d ' \t'
	@echo "== 0x5d property values vs vendor =="
	@dtc -I dtb -O dts $(VENDOR_5B) 2>/dev/null | sed -n '/ft6236@5d/,/};/p' \
	    | grep -E 'touchscreen|interrupts|reg|compatible' | tr -d ' \t' | sort > .vendor.props
	@dtc -I dtb -O dts $(OVERLAY_BIN) 2>/dev/null | sed -n '/gt911@5d/,/};/p' \
	    | grep -E 'touchscreen|interrupts|reg|compatible' | tr -d ' \t' | sort > .tracer.props
	@diff .vendor.props .tracer.props && echo "OK — identical, no value drift"
	@rm -f .vendor.props .tracer.props

SPLASH_TGA := image/splash/tracer-splash.tga

splash: $(SPLASH_TGA)

$(SPLASH_TGA): image/generate-splash.sh
	./image/generate-splash.sh

# Needs sudo — rpi-image-gen chroots the target rootfs. Depends on both build
# inputs so a stale splash or overlay can't silently end up in the image.
image: $(OVERLAY_BIN) $(SPLASH_TGA)
	@if [ "$$(id -u)" -ne 0 ]; then \
		echo "'make image' needs root: sudo make image"; exit 1; fi
	./image/build.sh

clean:
	rm -f $(OVERLAY_BIN) .vendor.props .tracer.props
	rm -f $(SPLASH_TGA) $(SPLASH_TGA:.tga=.png)

# Run the whole product on this machine — no hardware, no build step.
mock:
	cd tracerd && python3 -m tracerd --mock --ui-dir ../tracer-ui
	@true

# Push tracerd + GUI to the live board and restart. Seconds, not a rebuild.
dev:
	./scripts/dev-deploy.sh $(ARGS)

# A hand-provisioned dev board is missing everything the IMAGE installs as
# root, and every one of those gaps fails silently at runtime — which is how
# they kept getting mistaken for code bugs. dev-check is read-only.
dev-provision:
	./scripts/dev-provision.sh $(ARGS)

dev-check:
	./scripts/dev-provision.sh --check $(ARGS)

dev-mock:
	./scripts/dev-deploy.sh --mock

# Deploy AND show it on the board's panel.
dev-kiosk:
	./scripts/dev-deploy.sh --kiosk

# Capture what is actually on the panel -> ./panel.png
dev-shot:
	./scripts/dev-deploy.sh --shot

# Deploy AND make the board come up running Tracer at boot. Opt-in: a plain
# `make dev` must never change what the board does on power-up.
dev-autostart:
	./scripts/dev-deploy.sh --autostart

dev-no-autostart:
	./scripts/dev-deploy.sh --no-autostart

dev-stop:
	./scripts/dev-deploy.sh --stop

dev-logs:
	./scripts/dev-deploy.sh --logs

# Regenerate the offline Ionicons subset (needs npm i in tracer-ui).
icons:
	cd tracer-ui && node scripts/gen-icons.mjs

test:
	cd tracerd && python3 -m unittest discover -s tests -v
	@for f in tracer-ui/src/*.js tracer-ui/src/*/*.js; do node --check "$$f" || exit 1; done
	@echo "UI syntax OK"
# The render tests existed but nothing ran them, so a broken screen stayed
# green. A syntax check proves a file parses, not that it draws the right thing.
	@for t in tracer-ui/tests/*.test.mjs; do \
		echo "--- $$t"; node "$$t" || exit 1; \
	done

lint:
	@echo "'lint' is not implemented yet."
	@exit 1

flash:
	@echo "No 'make flash' — flashing writes to a raw block device and a wrong"
	@echo "answer destroys a disk, so it is deliberately manual."
	@echo "See docs/building.md#flash for the dd command and the lsblk check."
	@exit 1
