# Podpis App

Jednoduchá aplikácia na podpisovanie PDF dokumentov.

## Funkcie

- Nahraj ľubovoľný PDF dokument
- Aplikácia automaticky nájde miesto na podpis (podpisovú čiaru / pomocou AI ak nie je nájdená)
- Miesto podpisu je možné upraviť potiahnutím a zmenou veľkosti
- Vytvor odkaz na podpis pre druhú stranu, alebo podpíš priamo
- Podpis sa vloží do PDF na presné miesto
- Stiahnutie podpísaného PDF

## Nasadenie

1. Vytvor privátny GitHub Gist (slúži ako databáza) so súborom `documents_index.json` obsahujúcim `[]`
2. Nastav env premenné na Render:
   - `PODPIS_GIST_ID` - ID gistu
   - `PODPIS_GIST_TOKEN` - GitHub token s `gist` scope
   - `ANTHROPIC_API_KEY` - Claude API kľúč (pre fallback detekciu pozície podpisu)
3. Deploy ako Python web service (`uvicorn main:app --host 0.0.0.0 --port $PORT`)
