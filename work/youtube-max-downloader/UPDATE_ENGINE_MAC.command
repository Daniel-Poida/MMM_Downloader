#!/bin/zsh

SCRIPT_ROOT="${0:A:h}"
"$SCRIPT_ROOT/scripts/bootstrap_macos.sh" --update-only
STATUS=$?

if [[ $STATUS -eq 0 ]]; then
  print "Движок MMM Downloader обновлён. Нажмите любую клавишу, чтобы закрыть окно."
else
  print "Обновление завершилось с ошибкой. Нажмите любую клавишу, чтобы закрыть окно."
fi
read -k 1
exit $STATUS

