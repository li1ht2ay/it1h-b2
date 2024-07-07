FROM python:3.12

ENV ITCHCLAIM_DOCKER TRUE

WORKDIR /app

COPY requirements.txt .

RUN pip install -r requirements.txt

COPY . .

VOLUME [ "/data" ]

ENTRYPOINT [ "python", "itchclaim.py" ]
