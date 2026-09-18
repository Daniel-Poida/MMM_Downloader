//go:build windows

package main

import (
	_ "embed"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"unsafe"
)

const (
	applicationName    = "MMM Downloader"
	applicationVersion = "0.2.1"
	payloadRoot        = "MMM-Downloader-Windows"
	createNoWindow     = 0x08000000
	mbIconError        = 0x00000010
	mbIconInformation  = 0x00000040
	mbSetForeground    = 0x00010000
)

//go:embed payload.zip
var embeddedPayload []byte

func main() {
	if err := runApplication(); err != nil {
		messageBox(
			"MMM Downloader — ошибка",
			"Не удалось запустить приложение:\n\n"+err.Error(),
			mbIconError|mbSetForeground,
		)
		os.Exit(1)
	}
}

func runApplication() error {
	localAppData := strings.TrimSpace(os.Getenv("LOCALAPPDATA"))
	if localAppData == "" {
		configDir, err := os.UserConfigDir()
		if err != nil {
			return fmt.Errorf("не найдена пользовательская папка: %w", err)
		}
		localAppData = configDir
	}

	payloadHash := payloadSHA256(embeddedPayload)
	appDataRoot := filepath.Join(localAppData, applicationName)
	installDir := filepath.Join(
		appDataRoot,
		"application",
		applicationVersion+"-"+payloadHash[:12],
	)
	logPath := filepath.Join(appDataRoot, "logs", "bootstrap.log")
	firstRun := !payloadInstalled(installDir, payloadHash)
	if firstRun {
		if err := installPayload(embeddedPayload, installDir); err != nil {
			return fmt.Errorf("не удалось распаковать встроенные файлы: %w", err)
		}
		messageBox(
			applicationName,
			"Первый запуск подготовит локальные компоненты приложения.\n\n"+
				"Это может занять несколько минут. Подготовка идёт в фоне — дождитесь появления окна MMM Downloader.\n\n"+
				"Журнал: "+logPath,
			mbIconInformation|mbSetForeground,
		)
	}

	projectRoot := filepath.Join(installDir, payloadRoot)
	bootstrap := filepath.Join(projectRoot, "scripts", "bootstrap_windows.ps1")
	if _, err := os.Stat(bootstrap); err != nil {
		return fmt.Errorf("не найден встроенный сценарий запуска: %w", err)
	}
	powershell, err := findPowerShell()
	if err != nil {
		return err
	}

	if err := os.MkdirAll(filepath.Dir(logPath), 0o700); err != nil {
		return fmt.Errorf("не удалось создать папку журнала: %w", err)
	}
	logFile, err := os.OpenFile(logPath, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
	if err != nil {
		return fmt.Errorf("не удалось открыть журнал запуска: %w", err)
	}

	setup := exec.Command(
		powershell,
		"-NoLogo",
		"-NoProfile",
		"-NonInteractive",
		"-ExecutionPolicy",
		"Bypass",
		"-File",
		bootstrap,
		"-SkipLaunch",
	)
	setup.Dir = projectRoot
	setup.Stdout = logFile
	setup.Stderr = logFile
	setup.SysProcAttr = &syscall.SysProcAttr{
		HideWindow:    true,
		CreationFlags: createNoWindow,
	}
	err = setup.Run()
	closeErr := logFile.Close()
	if err != nil {
		details := readLogTail(logPath, 5000)
		if details == "" {
			details = err.Error()
		}
		return fmt.Errorf("подготовка среды завершилась с ошибкой:\n%s\n\nЖурнал: %s", details, logPath)
	}
	if closeErr != nil {
		return fmt.Errorf("не удалось сохранить журнал запуска: %w", closeErr)
	}

	runtimeRoot := filepath.Join(projectRoot, ".runtime")
	uvExecutable := filepath.Join(runtimeRoot, "uv", "uv.exe")
	denoRoot := filepath.Join(runtimeRoot, "deno")
	launchLog, err := os.OpenFile(logPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return fmt.Errorf("не удалось дополнить журнал запуска: %w", err)
	}
	defer launchLog.Close()

	command := exec.Command(uvExecutable, "run", "--no-sync", "mmm-downloader-gui")
	command.Dir = projectRoot
	command.Env = append(
		os.Environ(),
		"UV_CACHE_DIR="+filepath.Join(runtimeRoot, "uv-cache"),
		"UV_PYTHON_INSTALL_DIR="+filepath.Join(runtimeRoot, "python"),
		"UV_PYTHON_INSTALL_REGISTRY=0",
		"DENO_DIR="+filepath.Join(runtimeRoot, "deno-cache"),
		"PATH="+denoRoot+";"+os.Getenv("PATH"),
	)
	command.Stdout = launchLog
	command.Stderr = launchLog
	command.SysProcAttr = &syscall.SysProcAttr{
		HideWindow:    true,
		CreationFlags: createNoWindow,
	}
	if err := command.Run(); err != nil {
		return fmt.Errorf("интерфейс завершился с ошибкой: %w\n\nЖурнал: %s", err, logPath)
	}
	return nil
}

func readLogTail(path string, limit int) string {
	payload, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	if len(payload) > limit {
		payload = payload[len(payload)-limit:]
	}
	return strings.TrimSpace(string(payload))
}

func findPowerShell() (string, error) {
	if systemRoot := strings.TrimSpace(os.Getenv("SystemRoot")); systemRoot != "" {
		candidate := filepath.Join(
			systemRoot,
			"System32",
			"WindowsPowerShell",
			"v1.0",
			"powershell.exe",
		)
		if _, err := os.Stat(candidate); err == nil {
			return candidate, nil
		}
	}
	if candidate, err := exec.LookPath("powershell.exe"); err == nil {
		return candidate, nil
	}
	return "", fmt.Errorf("Windows PowerShell не найден")
}

func messageBox(title, message string, flags uintptr) {
	titleUTF16, titleErr := syscall.UTF16PtrFromString(title)
	messageUTF16, messageErr := syscall.UTF16PtrFromString(message)
	if titleErr != nil || messageErr != nil {
		return
	}
	user32 := syscall.NewLazyDLL("user32.dll")
	messageBoxW := user32.NewProc("MessageBoxW")
	_, _, _ = messageBoxW.Call(
		0,
		uintptr(unsafe.Pointer(messageUTF16)),
		uintptr(unsafe.Pointer(titleUTF16)),
		flags,
	)
}
