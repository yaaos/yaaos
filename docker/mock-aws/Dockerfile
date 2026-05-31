FROM golang:1.25-alpine AS build
WORKDIR /src
COPY go.mod .
COPY main.go .
RUN go build -o /mock-aws .

FROM alpine:3.21
COPY --from=build /mock-aws /mock-aws
EXPOSE 4566
ENTRYPOINT ["/mock-aws"]
