package dev.tenohost.permadeath;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.serializer.plain.PlainTextComponentSerializer;
import org.bukkit.BanList;
import org.bukkit.Bukkit;
import org.bukkit.GameMode;
import org.bukkit.Location;
import org.bukkit.Material;
import org.bukkit.Sound;
import org.bukkit.World;
import org.bukkit.block.Block;
import org.bukkit.block.Chest;
import org.bukkit.command.Command;
import org.bukkit.command.CommandExecutor;
import org.bukkit.command.CommandSender;
import org.bukkit.command.TabCompleter;
import org.bukkit.configuration.ConfigurationSection;
import org.bukkit.configuration.file.YamlConfiguration;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.entity.PlayerDeathEvent;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerRespawnEvent;
import org.bukkit.inventory.ItemStack;
import org.bukkit.inventory.PlayerInventory;
import org.bukkit.plugin.java.JavaPlugin;
import org.bukkit.scheduler.BukkitTask;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Date;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.TimeUnit;
import java.util.regex.Pattern;

public final class PermadeathPlugin extends JavaPlugin
        implements Listener, CommandExecutor, TabCompleter {
    private static final long DEFAULT_DURATION_SECONDS = 86_400;
    private static final long MAX_DURATION_SECONDS = TimeUnit.DAYS.toSeconds(36_500);
    private static final Pattern DISCORD_WEBHOOK = Pattern.compile(
            "^https://discord(?:app)?\\.com/api/webhooks/\\d+/[\\w-]+$");
    private static final PlainTextComponentSerializer PLAIN =
            PlainTextComponentSerializer.plainText();

    private final Map<UUID, DeathRecord> deaths = new HashMap<>();
    private final HttpClient http = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(5)).build();
    private YamlConfiguration deathData;
    private BukkitTask countdownTask;

    @Override
    public void onEnable() {
        saveDefaultConfig();
        getConfig().addDefault("enabled", false);
        getConfig().addDefault("revive-after-seconds", DEFAULT_DURATION_SECONDS);
        getConfig().options().copyDefaults(true);
        saveConfig();
        loadDeaths();
        getServer().getPluginManager().registerEvents(this, this);
        var command = getCommand("permadeath");
        if (command == null) {
            throw new IllegalStateException("Falta registrar /permadeath en plugin.yml");
        }
        command.setExecutor(this);
        command.setTabCompleter(this);
        countdownTask = getServer().getScheduler().runTaskTimer(this, this::updateCountdowns, 20L, 20L);
    }

    @Override
    public void onDisable() {
        if (countdownTask != null) {
            countdownTask.cancel();
        }
        saveDeaths();
    }

    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void onDeath(PlayerDeathEvent event) {
        if (!getConfig().getBoolean("enabled")) {
            return;
        }
        Player player = event.getPlayer();
        String cause = event.deathMessage() == null
                ? player.getName() + " died"
                : PLAIN.serialize(event.deathMessage());
        Location deathLocation = player.getLocation().clone();
        long expiresAt = System.currentTimeMillis() + durationMillis();

        List<ItemStack> items = copyInventory(player.getInventory());
        event.setKeepInventory(true);
        event.getDrops().clear();
        List<Location> chests = storeItems(deathLocation, items);
        clearInventory(player.getInventory());

        DeathRecord record = new DeathRecord(player.getName(), cause, expiresAt, deathLocation, chests);
        deaths.put(player.getUniqueId(), record);
        saveDeath(player.getUniqueId(), record);
        Bukkit.getBanList(BanList.Type.NAME).addBan(
                player.getName(), cause, new Date(expiresAt), "Permadeath");

        player.getWorld().playSound(deathLocation, Sound.ITEM_TRIDENT_THUNDER, 1.0f, 2.0f);
        player.getWorld().playSound(deathLocation, Sound.ITEM_TRIDENT_THUNDER, 1.0f, 0.0f);
        sendDeathWebhook(player.getName(), cause, deathLocation, expiresAt);
        getLogger().info("Permadeath: " + player.getName() + " murió: " + cause);
    }

    @EventHandler
    public void onRespawn(PlayerRespawnEvent event) {
        DeathRecord record = deaths.get(event.getPlayer().getUniqueId());
        if (record == null || record.expiresAt() <= System.currentTimeMillis()) {
            return;
        }
        event.setRespawnLocation(record.location().clone());
        Bukkit.getScheduler().runTask(this, () -> {
            Player player = event.getPlayer();
            DeathRecord current = deaths.get(player.getUniqueId());
            if (current != null && current.expiresAt() > System.currentTimeMillis()) {
                clearInventory(player.getInventory());
                player.setGameMode(GameMode.SPECTATOR);
                player.sendMessage(Component.text(
                        "§cHas muerto. Motivo del baneo: §f" + current.reason()));
                updateCountdown(player, current);
            }
        });
    }

    @EventHandler
    public void onJoin(PlayerJoinEvent event) {
        Player player = event.getPlayer();
        DeathRecord record = deaths.get(player.getUniqueId());
        if (record == null) {
            return;
        }
        if (record.expiresAt() <= System.currentTimeMillis()) {
            revive(player, record);
            return;
        }
        Bukkit.getScheduler().runTask(this, () -> {
            DeathRecord current = deaths.get(player.getUniqueId());
            if (current == null || current.expiresAt() <= System.currentTimeMillis()) {
                return;
            }
            clearInventory(player.getInventory());
            player.setGameMode(GameMode.SPECTATOR);
            player.teleport(current.location());
            player.sendMessage(Component.text(
                    "§cSigues en permadeath. Motivo del baneo: §f" + current.reason()));
            updateCountdown(player, current);
        });
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!sender.hasPermission("permadeath.admin")) {
            sender.sendMessage(Component.text("§cNo tienes permiso para administrar Permadeath."));
            return true;
        }
        if (args.length == 1 && args[0].equalsIgnoreCase("on")) {
            getConfig().set("enabled", true);
            saveConfig();
            sender.sendMessage(Component.text("§aPermadeath activado."));
            return true;
        }
        if (args.length == 1 && args[0].equalsIgnoreCase("off")) {
            getConfig().set("enabled", false);
            saveConfig();
            sender.sendMessage(Component.text("§ePermadeath desactivado. Los tiempos ya iniciados siguen."));
            return true;
        }
        if (args.length == 1 && args[0].equalsIgnoreCase("time")) {
            sender.sendMessage(Component.text("§eTiempo actual: "
                    + formatDuration(durationSeconds())
                    + ". Usa /permadeath time <cantidad> <d|h|m|s>."));
            return true;
        }
        if (args.length == 3 && args[0].equalsIgnoreCase("time")) {
            Long seconds = parseDuration(args[1], args[2]);
            if (seconds == null) {
                sender.sendMessage(Component.text(
                        "§cTiempo inválido. Ejemplos: /permadeath time 2 days, "
                                + "/permadeath time 6 hours, /permadeath time 30 minutes, "
                                + "/permadeath time 45 seconds."));
                return true;
            }
            getConfig().set("revive-after-seconds", seconds);
            saveConfig();
            sender.sendMessage(Component.text("§aTiempo para revivir: " + formatDuration(seconds) + "."));
            return true;
        }
        sender.sendMessage(Component.text(
                "§e/permadeath on | off | time [cantidad] [d|h|m|s]"));
        return true;
    }

    @Override
    public List<String> onTabComplete(
            CommandSender sender, Command command, String alias, String[] args) {
        if (args.length == 1) {
            return matching(List.of("on", "off", "time"), args[0]);
        }
        if (args.length == 2 && args[0].equalsIgnoreCase("time")) {
            return matching(List.of("1", "5", "10", "15", "30", "60", "120"), args[1]);
        }
        if (args.length == 3 && args[0].equalsIgnoreCase("time")) {
            return matching(List.of("seconds", "minutes", "hours", "days", "s", "m", "h", "d"), args[2]);
        }
        return List.of();
    }

    private List<ItemStack> copyInventory(PlayerInventory inventory) {
        List<ItemStack> items = new ArrayList<>();
        for (ItemStack item : inventory.getStorageContents()) {
            addCopy(items, item);
        }
        for (ItemStack item : inventory.getArmorContents()) {
            addCopy(items, item);
        }
        for (ItemStack item : inventory.getExtraContents()) {
            addCopy(items, item);
        }
        return items;
    }

    private void clearInventory(PlayerInventory inventory) {
        inventory.setStorageContents(new ItemStack[inventory.getStorageContents().length]);
        inventory.setArmorContents(new ItemStack[inventory.getArmorContents().length]);
        inventory.setExtraContents(new ItemStack[inventory.getExtraContents().length]);
    }

    private void addCopy(List<ItemStack> items, ItemStack item) {
        if (item != null && !item.getType().isAir()) {
            items.add(item.clone());
        }
    }

    private List<Location> storeItems(Location deathLocation, List<ItemStack> items) {
        List<Location> chests = new ArrayList<>();
        Location next = findChestLocation(deathLocation, SetHolder.EMPTY);
        if (next == null) {
            getLogger().severe("No se encontró espacio para el cofre de "
                    + deathLocation + "; los objetos se dejarán en el suelo.");
            for (ItemStack item : items) {
                deathLocation.getWorld().dropItemNaturally(deathLocation, item);
            }
            return chests;
        }
        int index = 0;
        while (index < items.size()) {
            Location chestLocation = next;
            Block block = chestLocation.getBlock();
            block.setType(Material.CHEST);
            if (!(block.getState() instanceof Chest chest)) {
                getLogger().severe("No se pudo crear el cofre en " + chestLocation);
                break;
            }
            chests.add(chestLocation.clone());
            Map<Integer, ItemStack> remaining = chest.getBlockInventory().addItem(
                    items.subList(index, items.size()).toArray(ItemStack[]::new));
            int stored = items.subList(index, items.size()).size() - remaining.size();
            index += stored;
            if (remaining.isEmpty()) {
                break;
            }
            next = findChestLocation(deathLocation, new SetHolder(chests));
            if (next == null) {
                getLogger().severe("No hay sitio para guardar todos los objetos de "
                        + deathLocation + "; los restantes se dejarán en el suelo.");
                for (ItemStack item : remaining.values()) {
                    deathLocation.getWorld().dropItemNaturally(deathLocation, item);
                }
                break;
            }
            items = new ArrayList<>(remaining.values());
            index = 0;
        }
        return chests;
    }

    private Location findChestLocation(Location origin, SetHolder used) {
        World world = origin.getWorld();
        if (world == null) {
            return null;
        }
        int baseX = origin.getBlockX();
        int baseY = origin.getBlockY();
        int baseZ = origin.getBlockZ();
        for (int radius = 0; radius <= 8; radius++) {
            for (int dx = -radius; dx <= radius; dx++) {
                for (int dz = -radius; dz <= radius; dz++) {
                    if (Math.max(Math.abs(dx), Math.abs(dz)) != radius) {
                        continue;
                    }
                    for (int dy : new int[]{0, 1, -1, 2}) {
                        Block block = world.getBlockAt(baseX + dx, baseY + dy, baseZ + dz);
                        Location location = block.getLocation();
                        if (used.contains(location) || !block.getType().isAir()) {
                            continue;
                        }
                        return location;
                    }
                }
            }
        }
        return null;
    }

    private void updateCountdowns() {
        long now = System.currentTimeMillis();
        for (Map.Entry<UUID, DeathRecord> entry : new ArrayList<>(deaths.entrySet())) {
            DeathRecord record = entry.getValue();
            Player player = Bukkit.getPlayer(entry.getKey());
            if (record.expiresAt() <= now) {
                if (player != null) {
                    revive(player, record);
                } else {
                    pardon(record.name());
                    deaths.remove(entry.getKey());
                    deathData.set(entry.getKey().toString(), null);
                    saveDeaths();
                }
            } else if (player != null) {
                updateCountdown(player, record);
            }
        }
    }

    private void updateCountdown(Player player, DeathRecord record) {
        player.sendActionBar(Component.text("§cRevives en §f" + formatDuration(
                Math.max(0, (record.expiresAt() - System.currentTimeMillis() + 999) / 1000))));
    }

    private void revive(Player player, DeathRecord record) {
        pardon(record.name());
        deaths.remove(player.getUniqueId());
        deathData.set(player.getUniqueId().toString(), null);
        saveDeaths();
        player.setGameMode(GameMode.SURVIVAL);
        Location location = findSafeReviveLocation(record.location());
        player.teleport(location);
        player.sendMessage(Component.text("§a¡Has revivido!"));
    }

    private Location findSafeReviveLocation(Location origin) {
        World world = origin.getWorld();
        if (world == null) {
            return Bukkit.getWorlds().getFirst().getSpawnLocation();
        }
        int baseX = origin.getBlockX();
        int baseY = origin.getBlockY();
        int baseZ = origin.getBlockZ();
        for (int radius = 0; radius <= 8; radius++) {
            for (int dx = -radius; dx <= radius; dx++) {
                for (int dz = -radius; dz <= radius; dz++) {
                    if (Math.max(Math.abs(dx), Math.abs(dz)) != radius) {
                        continue;
                    }
                    for (int dy = 4; dy >= -4; dy--) {
                        int feetY = baseY + dy;
                        if (feetY <= world.getMinHeight()
                                || feetY + 1 >= world.getMaxHeight()) {
                            continue;
                        }
                        Block floor = world.getBlockAt(baseX + dx, feetY - 1, baseZ + dz);
                        Material floorType = floor.getType();
                        if (!floorType.isSolid() || isDangerousFloor(floorType)
                                || !world.getBlockAt(baseX + dx, feetY, baseZ + dz)
                                .getType().isAir()
                                || !world.getBlockAt(baseX + dx, feetY + 1, baseZ + dz)
                                .getType().isAir()) {
                            continue;
                        }
                        return new Location(world, baseX + dx + 0.5, feetY, baseZ + dz + 0.5,
                                origin.getYaw(), origin.getPitch());
                    }
                }
            }
        }
        return world.getSpawnLocation().clone().add(0.5, 0, 0.5);
    }

    private boolean isDangerousFloor(Material material) {
        return switch (material) {
            case CACTUS, CAMPFIRE, FIRE, LAVA, MAGMA_BLOCK, POWDER_SNOW,
                    SOUL_CAMPFIRE, SOUL_FIRE, SWEET_BERRY_BUSH, WITHER_ROSE -> true;
            default -> false;
        };
    }

    private void pardon(String name) {
        Bukkit.getBanList(BanList.Type.NAME).pardon(name);
    }

    private void loadDeaths() {
        Path file = getDataFolder().toPath().resolve("deaths.yml");
        deathData = YamlConfiguration.loadConfiguration(file.toFile());
        ConfigurationSection section = deathData.getConfigurationSection("players");
        if (section == null) {
            return;
        }
        for (String id : section.getKeys(false)) {
            try {
                UUID uuid = UUID.fromString(id);
                ConfigurationSection saved = section.getConfigurationSection(id);
                if (saved == null) {
                    continue;
                }
                World world = Bukkit.getWorld(saved.getString("world", ""));
                if (world == null) {
                    continue;
                }
                Location location = new Location(world, saved.getDouble("x"), saved.getDouble("y"),
                        saved.getDouble("z"), (float) saved.getDouble("yaw"),
                        (float) saved.getDouble("pitch"));
                List<Location> chests = new ArrayList<>();
                for (String chestKey : saved.getStringList("chests")) {
                    String[] coords = chestKey.split(",", 4);
                    World chestWorld = Bukkit.getWorld(coords[0]);
                    if (chestWorld != null) {
                        chests.add(new Location(chestWorld, Integer.parseInt(coords[1]),
                                Integer.parseInt(coords[2]), Integer.parseInt(coords[3])));
                    }
                }
                DeathRecord record = new DeathRecord(saved.getString("name", "unknown"),
                        saved.getString("reason", "died"), saved.getLong("expires-at"),
                        location, chests);
                deaths.put(uuid, record);
                if (record.expiresAt() > System.currentTimeMillis()) {
                    Bukkit.getBanList(BanList.Type.NAME).addBan(record.name(), record.reason(),
                            new Date(record.expiresAt()), "Permadeath");
                }
            } catch (IllegalArgumentException ex) {
                getLogger().warning("Se omitió un registro de muerte inválido: " + id);
            }
        }
    }

    private void saveDeath(UUID uuid, DeathRecord record) {
        String key = "players." + uuid;
        deathData.set(key + ".name", record.name());
        deathData.set(key + ".reason", record.reason());
        deathData.set(key + ".expires-at", record.expiresAt());
        deathData.set(key + ".world", record.location().getWorld().getName());
        deathData.set(key + ".x", record.location().getX());
        deathData.set(key + ".y", record.location().getY());
        deathData.set(key + ".z", record.location().getZ());
        deathData.set(key + ".yaw", record.location().getYaw());
        deathData.set(key + ".pitch", record.location().getPitch());
        deathData.set(key + ".chests", record.chests().stream()
                .map(location -> location.getWorld().getName() + ","
                        + location.getBlockX() + "," + location.getBlockY() + ","
                        + location.getBlockZ()).toList());
        saveDeaths();
    }

    private void saveDeaths() {
        try {
            Files.createDirectories(getDataFolder().toPath());
            deathData.save(getDataFolder().toPath().resolve("deaths.yml").toFile());
        } catch (IOException ex) {
            getLogger().severe("No se pudieron guardar los tiempos de Permadeath: " + ex.getMessage());
        }
    }

    private void sendDeathWebhook(String player, String cause, Location location, long expiresAt) {
        String webhook = readWebhook();
        if (webhook.isEmpty()) {
            return;
        }
        JsonObject embed = new JsonObject();
        embed.addProperty("title", "☠ Permadeath");
        embed.addProperty("description", "**" + player + "** murió.\n\n"
                + "**Causa exacta:**\n" + cause);
        embed.addProperty("color", 0x9E1B32);
        embed.addProperty("timestamp", java.time.Instant.now().toString());
        JsonArray fields = new JsonArray();
        fields.add(field("Tiempo para revivir", formatDuration(
                Math.max(0, (expiresAt - System.currentTimeMillis() + 999) / 1000))));
        fields.add(field("Ubicación", location.getWorld().getName() + " "
                + location.getBlockX() + ", " + location.getBlockY() + ", " + location.getBlockZ()));
        embed.add("fields", fields);
        JsonObject image = new JsonObject();
        image.addProperty("url", "https://tenohost.pages.dev/plugins/permadeath.svg");
        embed.add("image", image);
        JsonArray embeds = new JsonArray();
        embeds.add(embed);
        JsonObject body = new JsonObject();
        body.add("embeds", embeds);
        JsonObject mentions = new JsonObject();
        mentions.add("parse", new JsonArray());
        body.add("allowed_mentions", mentions);

        HttpRequest request = HttpRequest.newBuilder(URI.create(webhook))
                .timeout(Duration.ofSeconds(10))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body.toString(), StandardCharsets.UTF_8))
                .build();
        http.sendAsync(request, HttpResponse.BodyHandlers.discarding())
                .thenAccept(response -> {
                    if (response.statusCode() < 200 || response.statusCode() >= 300) {
                        getLogger().warning("Discord rechazó el aviso de muerte (HTTP "
                                + response.statusCode() + ").");
                    }
                })
                .exceptionally(error -> {
                    getLogger().warning("No se pudo enviar el aviso de muerte a Discord: "
                            + error.getClass().getSimpleName());
                    return null;
                });
    }

    private String readWebhook() {
        Path envFile = getDataFolder().toPath().getParent().getParent().resolve(".env");
        if (!Files.isRegularFile(envFile)) {
            return "";
        }
        try {
            for (String line : Files.readAllLines(envFile, StandardCharsets.UTF_8)) {
                String trimmed = line.trim();
                if (!trimmed.startsWith("DISCORD_WEBHOOK=")) {
                    continue;
                }
                String url = trimmed.substring("DISCORD_WEBHOOK=".length()).trim()
                        .replaceAll("^[\"']|[\"']$", "");
                return DISCORD_WEBHOOK.matcher(url).matches() ? url : "";
            }
        } catch (IOException ex) {
            getLogger().warning("No se pudo leer DISCORD_WEBHOOK desde .env.");
        }
        return "";
    }

    private JsonObject field(String name, String value) {
        JsonObject field = new JsonObject();
        field.addProperty("name", name);
        field.addProperty("value", value);
        field.addProperty("inline", true);
        return field;
    }

    private String formatDuration(long seconds) {
        long days = seconds / 86_400;
        long hours = (seconds % 86_400) / 3_600;
        long minutes = (seconds % 3_600) / 60;
        long remainingSeconds = seconds % 60;
        List<String> parts = new ArrayList<>();
        if (days > 0) parts.add(days + " d");
        if (hours > 0) parts.add(hours + " h");
        if (minutes > 0) parts.add(minutes + " min");
        if (remainingSeconds > 0 || parts.isEmpty()) parts.add(remainingSeconds + " s");
        return String.join(" ", parts);
    }

    private long durationSeconds() {
        long seconds = getConfig().getLong("revive-after-seconds", DEFAULT_DURATION_SECONDS);
        if (seconds < 1 || seconds > MAX_DURATION_SECONDS) {
            getLogger().warning("La duración configurada no es válida; se usará 1 día.");
            getConfig().set("revive-after-seconds", DEFAULT_DURATION_SECONDS);
            saveConfig();
            return DEFAULT_DURATION_SECONDS;
        }
        return seconds;
    }

    private long durationMillis() {
        return TimeUnit.SECONDS.toMillis(durationSeconds());
    }

    private Long parseDuration(String amount, String unit) {
        try {
            long value = Long.parseLong(amount);
            if (value < 1) return null;
            long multiplier = switch (unit.toLowerCase()) {
                case "s", "second", "seconds", "segundo", "segundos" -> 1;
                case "m", "minute", "minutes", "minuto", "minutos" -> 60;
                case "h", "hour", "hours", "hora", "horas" -> 3_600;
                case "d", "day", "days", "dia", "dias" -> 86_400;
                default -> 0;
            };
            if (multiplier == 0 || value > MAX_DURATION_SECONDS / multiplier) return null;
            return value * multiplier;
        } catch (NumberFormatException ex) {
            return null;
        }
    }

    private List<String> matching(List<String> choices, String prefix) {
        String lower = prefix.toLowerCase();
        return choices.stream().filter(choice -> choice.toLowerCase().startsWith(lower)).toList();
    }

    private record DeathRecord(String name, String reason, long expiresAt, Location location,
                               List<Location> chests) {}

    private record SetHolder(List<Location> locations) {
        private static final SetHolder EMPTY = new SetHolder(List.of());

        boolean contains(Location location) {
            return locations.stream().anyMatch(existing ->
                    existing.getWorld().equals(location.getWorld())
                            && existing.getBlockX() == location.getBlockX()
                            && existing.getBlockY() == location.getBlockY()
                            && existing.getBlockZ() == location.getBlockZ());
        }
    }
}
