# 🧪 Bitácora de Experimentos (Team Experiment Log)

Usa esta tabla para registrar cada cambio, hipótesis y puntuación obtenida en Kaggle. Mantener esta bitácora actualizada evita repetir errores y ayuda a saber con certeza qué cambios realmente mejoran el agente.

---

### Registro de Envíos

| ID | Fecha | Autor | Branch / Commit | Hipótesis / Modificación Realizada | Validación Local | Kaggle Score | Conclusiones & Próximos Pasos |
| :---: | :---: | :---: | :---: | :--- | :---: | :---: | :--- |
| **#01** | 2026-09-27 | Adrian | `main` (`baseline`) | Configuración inicial basada en `sample_submission`. | ✅ Pasó | 0.0 | Punto de partida de referencia (baseline) para el equipo. |
| **#02** | 2026-10-01 | Adrian | `main` (v1.0) | **v1.0:** Orquestador solitario con flujo TDD (`/tmp/repro.py`), presupuestos ampliados (6 min / 30 tool calls) y reglas anti-patrones (bloqueo de bare pytest y archivos de test). | ✅ Pasó | 0.12 | Piso de rendimiento real (baseline en 0). |
| **#03** | 2026-10-01 | Adrian | `main` (v2.0) | **v2.0:** Inyección del sub-agente `semantic_scout` para mejorar la localización de código antes del paso de reproducción. | ✅ Pasó | 0.12 | Mantuvo el 0.12 (Zero Regression). La delegación a un subagente de búsqueda funcionó de forma estable sin degradar el rendimiento. |
| **#04** | 2026-10-03 | Adrian | `main` (v3.0) | **v3.0:** Diagnóstico dual de localización. Inyección del subagente `graph_inspector` (AST / callers / callees) coordinado tras el `semantic_scout` para extraer el bloque exacto (`old_string`) antes de editar. | ✅ Pasó | 0.08 | Caída a 0.08 (~2 tareas menos). La cadena forzada (Scout -> Inspector) en todas las tareas consume excesivo presupuesto de tiempo/turnos. |
| **#05** | 2026-10-03 | Adrian | `main` (v4.0) | **v4.0:** Arquitectura MoA completa. Desacoplamiento de la edición con el subagente `code_surgeon`. Orquestador actúa como puro planificador/verificador TDD con bucle de feedback. | ✅ Pasó | 0.05 | Caída a 0.05 (~3 tareas resueltas). Diagnóstico confirmado: cadena de 3 subagentes secuenciales rígidos agota el presupuesto de 6 min / 30 tools y quitar `edit_file` al orquestador lo deja atado de manos. |
| **#06** | 2026-10-05 | Adrian | `version/v4.1-ablation` | **v4.1 (Ablación):** Test de ablación sobre v4.0. Se devuelve `edit_file` al orquestador y se introduce Fast-Path (edición directa si el issue es obvio; especialistas como consultores de apoyo bajo demanda). | ✅ Pasó | 0.10 | Rebote de 0.05 a 0.10 (+100%). Confirma la hipótesis: desarmar al orquestador era el freno crítico. La brecha restante frente a 0.12 se debe a la sobrecarga de tener 3 subagentes en el menú de herramientas (dilución de atención y latencia innecesaria). |
| **#07** | 2026-10-06 | Adrian | `version/v2.1-lean` | **v2.1 (Lean Refined):** Intento de refinamiento con TDD híbrido y búsqueda de tests en `tests/`. | ✅ Pasó | 0.05 | Caída a 0.05. Diagnóstico forense: instruir al agente a buscar tests en `tests/` antes de reproducir causó que buscara tests inexistentes (el test de verificación viene en `test_patch` de Fase 2) o ejecutara tests rotos no relacionados. |
| **#08** | 2026-10-06 | Adrian | `version/v2.1-lean` | **v2.0 Restaurada:** Restauración exacta de v2.0 (Orquestador + Scout) para aislar la regresión de v2.1. | ✅ Pasó | 0.10 | Confirmación de estocasticidad con temp 0.2: el baseline fluctúa entre 6 y 7 tareas (0.10 - 0.12). |
| **#09** | 2026-10-08 | Adrian | `version/v5.0-lean-autonomous` | **v5.0 (Lean-Autonomous):** Orquestador autónomo con 6 herramientas esenciales (poda de grafos y scout). Calibración de presupuestos (10 min / 50 calls / 180s timeout), sampling E11/E12 (temp 1.0, top_p 0.95, seed 42) y prompt con protocolo anti-FileEditError, priorización de hints y TDD blindado. | ✅ Pasó | - | Enviado a Kaggle (en evaluación). |
| **#10** | 2026-10-10 | Adrian | `version/v5.1-robust-hygiene` | **v5.1 (Robust-Hygiene & Shortlist):** Calibración de tiempo a 6.0 min y timeout a 150s (ventana 12h Kaggle). Localización en 2 pasos (pre-filtrado git grep -l/c), cheatsheet arquitectónico, protocolo dual de edición (evita mangling vLLM) y Patch Hygiene pre-submit obligatoria (revertir tests). | Pendiente | - | En preparación para validación y empaquetado. |

---

### Guía para Completar Cada Fila:
* **ID:** Número correlativo del experimento (#01, #02, etc.).
* **Autor:** Quién preparó o envió el cambio.
* **Branch / Commit:** Hash del commit o nombre de la rama (ej. `feat/syntax-prompt`).
* **Hipótesis:** ¿Por qué creemos que este cambio mejorará el puntaje? (Ej. *"Añadir instrucción para aislar scripts en /tmp"*).
* **Validación Local:** ¿Se ejecutó `python scripts/validate.py` sin errores?
* **Kaggle Score:** Resolución en Kaggle (tasa de éxito entre 0.0 y 1.0).
* **Conclusiones:** ¿Subió o bajó el puntaje? ¿Qué aprendimos?
