# 🤖 Gemma 4 Developer Agent — Repositorio del Equipo

Repositorio colaborativo para participar en la competencia **Gemma 4 Developer Agent Competition** en Kaggle.

---

## 📁 Estructura del Repositorio

```text
gemma-agent/
├── .github/workflows/
│   └── validate_and_pack.yml    # CI: Valida y compila submission.zip en cada PR y push
├── src/                         # Código declarativo del agente (lo que entra al ZIP)
│   ├── agent.yaml               # Agente principal (gemma-4-31b-it-qat-w4a16-ct)
│   ├── eval_config.yaml         # Presupuestos por tarea (timeouts, max_turns, max_tool_calls)
│   ├── configs/                 # Configuración de sampling (temperature, thinking_budget)
│   ├── prompts/                 # Prompts en Markdown (system.md, analyzer.md)
│   ├── sub_agents/              # Subagentes especializados (code_analyzer.yaml)
│   ├── adapters/                # Pesos de adaptadores LoRA (PEFT .safetensors)
│   └── skills/                  # Habilidades compuestas ADK (opcional)
├── scripts/
│   ├── validate.py              # Validador de reglas oficiales de ADK
│   ├── package.py               # Empaquetador automático a dist/submission.zip
│   └── submit.py                # Envío automático a Kaggle vía CLI
├── dist/                        # Carpeta local para almacenar submission.zip (ignorada en git)
├── EXPERIMENTS.md               # Bitácora compartida de experimentos y puntajes
└── requirements.txt             # Dependencias locales para desarrollo
```

---

## 🚀 Inicio Rápido (Instrucciones para el Equipo)

### 1. Clonar e Instalar Dependencias Locales
```bash
git clone https://github.com/AMonFer/Gemma4-Developer-Agent-Competition.git
cd Gemma4-Developer-Agent-Competition
pip install -r requirements.txt
```

### 2. Flujo de Trabajo en Ramas (Para no pisarse)
1. **Crear una rama con tu nombre y el cambio:**
   ```bash
   git checkout -b <tu-nombre>/<mejora>
   # Ejemplo:
   git checkout -b adrian/mejorar-prompt-debug
   ```
2. Realizar los cambios dentro de `src/` (modificar prompts, subagentes o configs).
3. **Validar localmente:**
   ```bash
   python scripts/validate.py
   ```
4. **Hacer commit y subir la rama a GitHub:**
   ```bash
   git add src/
   git commit -m "feat: optimizado prompt de diagnóstico en analyzer.md"
   git push -u origin adrian/mejorar-prompt-debug
   ```
5. Abrir un **Pull Request (PR)** en GitHub hacia `main`.
   * GitHub Actions validará automáticamente tu PR y generará el archivo `submission.zip` para descarga en la pestaña de Actions.
6. Una vez revisado y aprobado, hacer merge a `main`.

---

## 📦 Empaquetado y Envío Oficial

### Opción A: Empaquetar Localmente
```bash
python scripts/package.py
```
* Generará `dist/submission.zip` garantizando que `agent.yaml` quede en la raíz del archivo ZIP y verificando el hash SHA256.
* Puedes subir ese archivo directamente a la plataforma de Kaggle.

### Opción B: Envío Directo por Terminal (Kaggle API)
Una vez configurado tu token `kaggle.json` en `~/.kaggle/`:
```bash
python scripts/submit.py -m "Experimento #02: Subagente de análisis con thinking_budget 2048"
```

---

## ⚠️ Reglas Críticas del Arnés (¡No olvidar!)
1. **Archivos temporales en el agente:** Todo script de prueba generado por el agente dentro de `/workspace` debe crearse en `/tmp/` para no ensuciar el parche de Git.
2. **No modificar tests:** No tocar archivos dentro de `tests/` ni configuraciones de `pytest.ini`.
3. **Límite de tamaño:** La carpeta descomprimida no debe superar los 3 GiB.
4. **Modelo único:** Siempre usar `gemma-4-31b-it-qat-w4a16-ct` en todos los agentes antes del envío.
