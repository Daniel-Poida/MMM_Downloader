package main

import (
	"archive/zip"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

const markerFilename = ".mmm-payload-sha256"

func payloadSHA256(payload []byte) string {
	digest := sha256.Sum256(payload)
	return hex.EncodeToString(digest[:])
}

func payloadInstalled(destination, expectedHash string) bool {
	payload, err := os.ReadFile(filepath.Join(destination, markerFilename))
	return err == nil && strings.TrimSpace(string(payload)) == expectedHash
}

func installPayload(payload []byte, destination string) error {
	if err := os.MkdirAll(destination, 0o700); err != nil {
		return fmt.Errorf("create application directory: %w", err)
	}
	if err := extractZip(payload, destination); err != nil {
		return err
	}
	hash := payloadSHA256(payload)
	if err := os.WriteFile(filepath.Join(destination, markerFilename), []byte(hash+"\n"), 0o600); err != nil {
		return fmt.Errorf("write installation marker: %w", err)
	}
	return nil
}

func extractZip(payload []byte, destination string) error {
	reader, err := zip.NewReader(bytes.NewReader(payload), int64(len(payload)))
	if err != nil {
		return fmt.Errorf("open embedded application: %w", err)
	}

	root, err := filepath.Abs(destination)
	if err != nil {
		return fmt.Errorf("resolve application directory: %w", err)
	}
	for _, entry := range reader.File {
		name := filepath.Clean(filepath.FromSlash(entry.Name))
		if name == "." {
			continue
		}
		if filepath.IsAbs(name) || name == ".." || strings.HasPrefix(name, ".."+string(os.PathSeparator)) {
			return fmt.Errorf("unsafe embedded path: %q", entry.Name)
		}
		if entry.Mode()&os.ModeSymlink != 0 {
			return fmt.Errorf("embedded symlinks are not allowed: %q", entry.Name)
		}

		target := filepath.Join(root, name)
		relative, err := filepath.Rel(root, target)
		if err != nil || relative == ".." || strings.HasPrefix(relative, ".."+string(os.PathSeparator)) {
			return fmt.Errorf("unsafe embedded path: %q", entry.Name)
		}
		if entry.FileInfo().IsDir() {
			if err := os.MkdirAll(target, 0o700); err != nil {
				return fmt.Errorf("create embedded directory: %w", err)
			}
			continue
		}
		if err := extractZipFile(entry, target); err != nil {
			return err
		}
	}
	return nil
}

func extractZipFile(entry *zip.File, target string) error {
	if err := os.MkdirAll(filepath.Dir(target), 0o700); err != nil {
		return fmt.Errorf("create parent directory: %w", err)
	}
	source, err := entry.Open()
	if err != nil {
		return fmt.Errorf("open embedded file %q: %w", entry.Name, err)
	}
	defer source.Close()

	destination, err := os.OpenFile(target, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
	if err != nil {
		return fmt.Errorf("create embedded file %q: %w", entry.Name, err)
	}
	_, copyErr := io.Copy(destination, source)
	closeErr := destination.Close()
	if copyErr != nil {
		return fmt.Errorf("extract embedded file %q: %w", entry.Name, copyErr)
	}
	if closeErr != nil {
		return fmt.Errorf("finish embedded file %q: %w", entry.Name, closeErr)
	}
	return nil
}
