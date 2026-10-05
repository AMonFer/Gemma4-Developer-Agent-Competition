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
| **#06** | - | - | - | - | - | - | - |
| **#07** | - | - | - | - | - | - | - |

---

### Guía para Completar Cada Fila:
* **ID:** Número correlativo del experimento (#01, #02, etc.).
* **Autor:** Quién preparó o envió el cambio.
* **Branch / Commit:** Hash del commit o nombre de la rama (ej. `feat/syntax-prompt`).
* **Hipótesis:** ¿Por qué creemos que este cambio mejorará el puntaje? (Ej. *"Añadir instrucción para aislar scripts en /tmp"*).
* **Validación Local:** ¿Se ejecutó `python scripts/validate.py` sin errores?
* **Kaggle Score:** Resolución en Kaggle (tasa de éxito entre 0.0 y 1.0).
* **Conclusiones:** ¿Subió o bajó el puntaje? ¿Qué aprendimos?
