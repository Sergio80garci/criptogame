---
name: report-changes
description: Comunicar cambios de código de forma clara cada vez que se modifica el proyecto crypto_jev. Usar siempre que se edite, cree o borre un archivo del repo, o cuando se detecte que otra herramienta (Codex u otra) modificó archivos desde la última lectura.
---

# Reportar cambios (crypto_jev)

Este proyecto lo trabaja el usuario con más de un asistente (Claude Code y
Codex, al menos). Por eso:

1. **Antes de asumir el estado de un archivo**, verificar si cambió en disco
   desde la última lectura (el harness ya avisa esto vía system-reminder
   cuando aplica). Si cambió por fuera, leerlo completo y tratar ese cambio
   como deliberado — no revertirlo sin preguntar, y mencionarlo brevemente
   si es relevante para lo que se está por hacer.

2. **Después de cada cambio propio** (crear/editar/borrar archivos, instalar
   dependencias, tocar configuración), resumir en la respuesta al usuario:
   - qué archivo(s) cambiaron y en qué carpeta
   - qué hace el cambio en una o dos frases, en español simple
   - si algo quedó pendiente de probar o de una decisión del usuario (ej.
     API keys, credenciales, elecciones de diseño)

3. Preferir listas cortas o rutas tipo `archivo.ext` sobre párrafos largos
   cuando el cambio es técnico — el usuario prefiere ver rápido qué tocó
   y por qué, no una narración extensa.

4. Si el cambio es puramente exploratorio (leer código, probar un endpoint,
   investigar documentación) sin modificar nada, no hace falta el resumen
   de "cambios" — solo aplica cuando algo del proyecto quedó distinto.
