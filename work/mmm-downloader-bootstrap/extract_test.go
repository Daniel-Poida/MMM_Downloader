package main

import (
	"archive/zip"
	"bytes"
	"os"
	"path/filepath"
	"testing"
)

func zipPayload(t *testing.T, name, contents string) []byte {
	t.Helper()
	var buffer bytes.Buffer
	writer := zip.NewWriter(&buffer)
	file, err := writer.Create(name)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := file.Write([]byte(contents)); err != nil {
		t.Fatal(err)
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	return buffer.Bytes()
}

func TestInstallPayloadAndMarker(t *testing.T) {
	payload := zipPayload(t, "MMM-Downloader-Windows/README.md", "hello")
	destination := t.TempDir()
	if err := installPayload(payload, destination); err != nil {
		t.Fatal(err)
	}
	contents, err := os.ReadFile(filepath.Join(destination, "MMM-Downloader-Windows", "README.md"))
	if err != nil {
		t.Fatal(err)
	}
	if string(contents) != "hello" {
		t.Fatalf("unexpected contents: %q", contents)
	}
	if !payloadInstalled(destination, payloadSHA256(payload)) {
		t.Fatal("payload marker was not written")
	}
}

func TestExtractRejectsTraversal(t *testing.T) {
	payload := zipPayload(t, "../outside.txt", "bad")
	if err := extractZip(payload, t.TempDir()); err == nil {
		t.Fatal("expected traversal path to be rejected")
	}
}
