# Wantlist + MTGTop8 analýza

## Duel Commander: Slimefoot and Squee

Skript `mtgtop8_analysis.py` stáhne top 8 decklisty archetypu Slimefoot and Squee z MTGTop8, uloží každý deck do samostatného textového souboru a porovná vzorek s `decks/duel-slimfoot`.

První spuštění bez parametrů analyzuje turnaje od 1. ledna aktuálního roku:

```sh
python3 mtgtop8_analysis.py
```

Konkrétní začátek období lze nastavit explicitně:

```sh
python3 mtgtop8_analysis.py --since 2026-01-01
```

Výstup je `duel-slimfoot-analysis.html`. Lokální databáze je v `mtgtop8-cache/slimefoot-and-squee/`: metadata běhu jsou v `state.json` a čitelné decklisty v `decks/*.txt`. Cache je záměrně v `.gitignore`.

Při dalším spuštění se projdou jen výsledky od data poslední úspěšné analýzy (datum se bere inkluzivně, aby se zachytily pozdě přidané decky ze stejného dne). Deck ID z MTGTop8 slouží k deduplikaci. Starší cache zůstává součástí analýzy.

Užitečné volby:

```sh
# Pouze přegenerovat HTML z lokální cache
python3 mtgtop8_analysis.py --offline

# Znovu projít celé zvolené období; existující decklisty se znovu nestahují
python3 mtgtop8_analysis.py --refresh-all

# Změnit deck nebo výstup
python3 mtgtop8_analysis.py --deck decks/duel-slimfoot --output duel-slimfoot-analysis.html
```

Report uvádí prostou i váženou hranost a dělí karty do skupin Commander, země, bytosti, instanty, sorcery, artefakty, enchantmenty, planeswalkeři, battles a ostatní. Přímo v HTML lze přepnout celé období, posledních 30 nebo 90 dní, aktuální rok nebo zadat vlastní datum. Procenta, pořadí karet, souhrny i návrhy výměn se přepočítají bez nového stahování.

Váha zvýhodňuje větší turnaje, lepší umístění a mírně také úroveň turnaje uvedenou na MTGTop8. Návrhy výměn jsou čistě statistické, párují pouze stejné široké typy karet a nejsou náhradou za posouzení synergií nebo lokální mety.

Testy:

```sh
python3 -m unittest discover -s tests -v
```
