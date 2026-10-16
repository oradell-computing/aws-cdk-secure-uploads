// Browser script for the text-fingerprint demo. Plain JavaScript, no dependencies.
// The checks here are only for faster feedback; S3 enforces size, type and expiry
// through the presigned POST conditions. Everything shown uses textContent.
"use strict";

const MAX_BYTES = 100 * 1024;
const POLL_INTERVAL_MS = 1000;
const MAX_POLLS = 30;

const fileInput = document.getElementById("file");
const uploadButton = document.getElementById("upload");
const statusEl = document.getElementById("status");
const outputEl = document.getElementById("output");

function showStatus(message, isError) {
  statusEl.textContent = message;
  statusEl.className = isError ? "error" : "";
}

function clearResult() {
  outputEl.replaceChildren();
}

// Every value from the server, including words from the visitor's own file, is put on
// the page with textContent, so markup in a file is displayed as text and never runs.
function element(tag, text) {
  const node = document.createElement(tag);
  node.textContent = text;
  return node;
}

function formatReadingTime(totalSeconds) {
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return minutes > 0 ? minutes + " min " + seconds + " s" : seconds + " s";
}

function showResult(fingerprint) {
  clearResult();
  const rows = [
    ["SHA-256", fingerprint.sha256],
    ["Size", fingerprint.bytes + " bytes"],
    ["Words", String(fingerprint.word_count)],
    ["Unique words", String(fingerprint.unique_words)],
    ["Sentences", String(fingerprint.sentence_count)],
    ["Average sentence length", fingerprint.avg_sentence_length + " words"],
    ["Reading time", formatReadingTime(fingerprint.reading_time_seconds)],
  ];
  const list = document.createElement("dl");
  for (const [label, value] of rows) {
    list.append(element("dt", label), element("dd", value));
  }

  const topWords = document.createElement("ol");
  for (const entry of fingerprint.top_words) {
    topWords.append(element("li", entry.word + " \u2014 " + entry.count));
  }
  outputEl.append(list, element("h2", "Top words"), topWords);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function requestUploadLink() {
  const response = await fetch("/uploads", { method: "POST" });
  if (!response.ok) {
    throw new Error("Could not get an upload link (HTTP " + response.status + ").");
  }
  return response.json();
}

async function uploadToS3(link, file) {
  const form = new FormData();
  for (const [name, value] of Object.entries(link.fields)) {
    form.append(name, value);
  }
  form.append("file", file); // S3 requires the file to be the last field
  const response = await fetch(link.url, { method: "POST", body: form });
  if (!response.ok) {
    throw new Error("The upload was rejected (HTTP " + response.status + ").");
  }
}

async function pollForResult(id) {
  for (let attempt = 0; attempt < MAX_POLLS; attempt++) {
    const response = await fetch("/results/" + encodeURIComponent(id));
    if (!response.ok) {
      throw new Error("Could not fetch the result (HTTP " + response.status + ").");
    }
    const result = await response.json();
    if (result.status !== "pending") {
      return result;
    }
    await sleep(POLL_INTERVAL_MS);
  }
  throw new Error("Processing failed or timed out. Please try again.");
}

async function run() {
  clearResult(); // a new attempt never shows the previous file's result
  const file = fileInput.files[0];
  if (!file) {
    showStatus("Choose a .txt file first.", true);
    return;
  }
  if (!file.name.toLowerCase().endsWith(".txt")) {
    showStatus("Only .txt files are accepted.", true);
    return;
  }
  if (file.size === 0 || file.size > MAX_BYTES) {
    showStatus("The file must be between 1 byte and 100 KB.", true);
    return;
  }

  uploadButton.disabled = true;
  try {
    showStatus("Requesting an upload link...", false);
    const link = await requestUploadLink();
    showStatus("Uploading...", false);
    await uploadToS3(link, file);
    showStatus("Processing...", false);
    const result = await pollForResult(link.id);
    if (result.status === "ok") {
      showStatus("Done.", false);
      showResult(result.fingerprint);
    } else {
      clearResult();
      showStatus(result.error || "The file could not be processed.", true);
    }
  } catch (error) {
    clearResult();
    showStatus(error.message || "Something went wrong. Please try again.", true);
  } finally {
    uploadButton.disabled = false;
  }
}

uploadButton.addEventListener("click", run);
