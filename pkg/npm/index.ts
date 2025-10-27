#!/usr/bin/env node

import { execSync } from "child_process";

const ensureUv = () => {
	// check if 'uv' is a valid executable in the PATH
	// then run 'uv --version' to verify it's working
	// if not found, throw an error
	try {
		const version = execSync("uv --version", { encoding: "utf-8" });
		console.log(`uv is installed. Version: ${version}`);
	} catch (error) {
		throw new Error("uv executable not found in PATH. Please install uv.");
	}
};

const ensureUvx = () => {
	// check if 'uvx' is a valid executable in the PATH
	// then run 'uvx --version' to verify it's working
	// if not found, throw an error
	try {
		const version = execSync("uvx --version", { encoding: "utf-8" });
		console.log(`uvx is installed. Version: ${version}`);
	} catch (error) {
		throw new Error("uvx executable not found in PATH. Please install uvx.");
	}
};

const ensureStelvio = () => {
	// check if 'stelvio' is a valid executable in the PATH
	// then run 'stelvio --version' to verify it's working
	// if not found, throw an error
	try {
		execSync("uvx --from stelvio stlv --help", { encoding: "utf-8" });
	} catch (error) {
		throw new Error(
			"stelvio executable not found in PATH. Please install stelvio.",
		);
	}
};

const runStelvioCommand = (args: string[]) => {
	try {
		const command = `uvx --from stelvio stlv ${args.join(" ")}`;
		const output = execSync(command, { encoding: "utf-8" });
		console.log(output);
	} catch (error) {
		console.error("Error running stelvio command:", error);
	}
};

const main = () => {
	ensureUv();
	ensureUvx();
	ensureStelvio();
	const args = process.argv.slice(2);
	runStelvioCommand(args);
};
main();
