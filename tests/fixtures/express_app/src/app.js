const express = require("express");
const usersRouter = require("./routes/users");
const { reportsRouter } = require("./routes/reports");

const app = express();

app.get("/health", (req, res) => res.json({ ok: true }));
// app.get("/old-route", (req, res) => res.send("gone"));
/*
app.post("/block-commented", handler);
*/
app.use("/users", usersRouter);
app.use("/api/reports", reportsRouter);

const note = "app.get('/in-string', h) is not a route";

app.listen(3000);
