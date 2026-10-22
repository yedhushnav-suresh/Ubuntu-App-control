.PHONY: all build release test run install uninstall deb clean

all: build

build:
	cargo build --locked

release:
	cargo build --locked --release

test:
	cargo test --locked

run:
	cargo run --locked

install:
	./install.sh

uninstall:
	./uninstall.sh

deb:
	./build-deb.sh

clean:
	cargo clean
	rm -rf build dist
