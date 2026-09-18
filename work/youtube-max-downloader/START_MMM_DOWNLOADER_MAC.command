#!/bin/zsh

SCRIPT_ROOT="${0:A:h}"
"$SCRIPT_ROOT/scripts/bootstrap_macos.sh"
STATUS=$?

if [[ $STATUS -ne 0 ]]; then
  print ""
  print "Запуск завершился с ошибкой. Нажмите любую клавишу, чтобы закрыть окно."
  read -k 1
fi

exit $STATUS

