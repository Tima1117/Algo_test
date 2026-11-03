# Весь путь от сбора до готового сайта: make all
PY ?= python3

.PHONY: all install collect build test site clean-raw

all: install collect site            ## сбор → данные → сайт

install:
	$(PY) -m pip install -e '.[dev]'
	cd site && npm ci

collect:                            ## реестр + сбор + нормализация + притоки + отчёт качества
	$(PY) -m pif all --strict

build:                              ## только пересборка данных из уже собранного (без сети)
	$(PY) -m pif build

test:
	$(PY) -m pytest -q

site:                               ## статический сайт в site/dist/pif/
	cd site && npm run build

clean-raw:                          ## удалить кэш сырых ответов (их восстановит следующий сбор)
	rm -rf raw
