/**
 * @fileoverview A utility to test that ESLint doesn't crash with EMFILE/ENFILE errors.
 * @author Nicholas C. Zakas
 *
 * Modified for Harbor test environment to work with reduced file limits.
 */

"use strict";

//------------------------------------------------------------------------------
// Requirements
//------------------------------------------------------------------------------

const fs = require("fs");
const { execSync } = require("child_process");

//------------------------------------------------------------------------------
// Helpers
//------------------------------------------------------------------------------

const OUTPUT_DIRECTORY = "tmp/emfile-check";
const CONFIG_DIRECTORY = "tests/fixtures/emfile";

/*
 * We use a reasonable file count that should exceed the soft ulimit we set,
 * forcing EMFILE errors that ESLint should handle with retry logic.
 */
const FILE_COUNT = 2000;

/**
 * Generates files in a directory.
 * @returns {void}
 */
function generateFiles() {
    fs.mkdirSync(OUTPUT_DIRECTORY, { recursive: true });

    for (let i = 0; i < FILE_COUNT; i++) {
        const fileName = `file_${i}.js`;
        const fileContent = `// This is file ${i}`;

        fs.writeFileSync(`${OUTPUT_DIRECTORY}/${fileName}`, fileContent);
    }
}

//------------------------------------------------------------------------------
// Main
//------------------------------------------------------------------------------

console.log(`Generating ${FILE_COUNT} files in ${OUTPUT_DIRECTORY}...`);
generateFiles();

console.log("Running ESLint...");
execSync(`node bin/eslint.js ${OUTPUT_DIRECTORY} -c ${CONFIG_DIRECTORY}/eslint.config.js`, { stdio: "inherit" });
console.log("No errors encountered running ESLint.");
