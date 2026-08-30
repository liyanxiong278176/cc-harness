# ---------- 构建阶段 ----------
FROM maven:3.9-eclipse-temurin-17 AS build
WORKDIR /build
# 先复制 pom 与源码,利用层缓存
COPY pom.xml .
COPY campus-common/pom.xml campus-common/
COPY campus-dao/pom.xml campus-dao/
COPY campus-service/pom.xml campus-service/
COPY campus-web/pom.xml campus-web/
COPY campus-common/src campus-common/src/
COPY campus-dao/src campus-dao/src/
COPY campus-service/src campus-service/src/
COPY campus-web/src campus-web/src/
# 离线优先: 若本地仓库已预热则加 -o;首次构建需联网拉取依赖
RUN mvn -q -pl campus-web -am package -DskipTests

# ---------- 运行阶段 ----------
FROM eclipse-temurin:17-jre
# 安装 curl 供 docker-compose healthcheck 探活
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /build/campus-web/target/campus-web-*.jar app.jar
EXPOSE 8080
ENTRYPOINT ["java", "-XX:MaxRAMPercentage=75", "-jar", "app.jar"]
