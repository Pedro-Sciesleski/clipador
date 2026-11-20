Como rodar o Clipador

1. Instale o Python
Baixe em python.org/downloads. Na primeira tela do instalador, marque “Add Python to PATH”.

2. Instale o ffmpeg e as ferramentas
Abra o Terminal (tecla Windows, digite “terminal” e dê Enter) e rode um comando de cada vez:

winget install --id Gyan.FFmpeg -e
python -m pip install -U yt-dlp streamlink

Depois feche o terminal e abra de novo.

3. Confira se deu certo

python --version
ffmpeg -version
python -m yt_dlp --version

Os três precisam mostrar um número de versão. Se o ffmpeg der erro, reinicie o PC.

4. Coloque os scripts numa pasta
Crie a pasta C:\clipador e coloque dentro dela o clipador.py e o clipador_video.py. Os nomes precisam estar exatamente assim, sem “(1)” e sem “.txt” no final.

5. Rode

cd C:\clipador
Live acontecendo agora (YouTube ou Twitch):
python clipador.py "LINK_DA_LIVE"

Ele corta sozinho enquanto a live rola. ENTER corta na hora e Ctrl+C para.

Vídeo ou live que já terminou:
python clipador_video.py "LINK_DO_VIDEO"

Os cortes aparecem em C:\clipador\cortes.

Se algo der errado:

Vieram poucos cortes: adicione --min-score 1.0 no fim do comando.
O YouTube bloqueia o download: adicione --cookies-do-navegador chrome no fim do comando.
O YouTube começou a falhar de repente: rode python -m pip install -U yt-dlp.
