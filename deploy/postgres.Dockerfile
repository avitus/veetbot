# Keep the existing PostgreSQL 16 Alpine locale/volume environment. Only add
# the versioned vector extension; do not implicitly switch libc/collations.
FROM postgres:16-alpine AS vector-build
RUN apk add --no-cache build-base curl \
    && curl -fLsS https://github.com/pgvector/pgvector/archive/refs/tags/v0.8.7.tar.gz -o /tmp/vector.tar.gz \
    && echo 'cac0b10c360f05b2d521200105ba3697e773d4cd3731f5a915a7e37ebe0bea85  /tmp/vector.tar.gz' | sha256sum -c - \
    && tar -xzf /tmp/vector.tar.gz -C /tmp \
    && make -C /tmp/pgvector-0.8.7 OPTFLAGS="" with_llvm=no \
    && make -C /tmp/pgvector-0.8.7 with_llvm=no DESTDIR=/vector install

FROM postgres:16-alpine
COPY --from=vector-build /vector/usr/local/ /usr/local/
